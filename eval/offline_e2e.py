"""Офлайн-e2e: конвейер /faq/ask без бэкенда (предпросмотр E4, E5 и Р1).

    python -m eval.offline_e2e --corpus corpus/ --dataset eval/private/golden.csv \\
        --limit 5

По умолчанию — конфигурация, выбранная по задаче 1 (25.09): Alice AI LLM
Flash через OpenAI-совместимый API, text-embeddings-v2 с размерностью 768,
порог 0.51. Старая: --api native --model yandexgpt-lite
--embedding-model text-search --max-distance 0.65.

Официальные числа — через run_eval e2e (HTTP API бэкенда). Этот стенд
нужен, пока API нет, и для перебора того, что на бэкенде меняется только
переменными окружения: faq_limit (E4), модель (E5), порог (A8), режим
«ответа нет» (Р1).

Шаги повторяют бэкенд (docs/backend-handoff.md, BH-3 и BH-7):
1. top-limit чанков: по косинусному расстоянию (<семейство>-query → doc)
   или гибрид вектор + BM25 через RRF (--retriever hybrid, предпросмотр M1);
2. порог: для вектора — каждый чанк дальше max_distance отбрасывается
   (как faq_service сейчас); для гибрида — по лучшему векторному
   кандидату (контракт M1). Не осталось ни одного — ответ по выдержкам не
   вызывается: общий ответ с пометкой (general, Р1 — по умолчанию) или
   фраза отказа (strict);
3. select_context — бюджет контекста в токенах;
4. build_faq_messages (промпт PROMPT_VERSION) → LLM →
   normalize_citations ([4.2] → номер выдержки), как должен делать бэкенд;
5. general: модель по выдержкам ответила отказом (is_not_found) — второй
   вызов, общий ответ с пометкой (колонка general_after_refusal).

CSV совместим с run_eval score и eval.judge: те же колонки, что у
run_eval e2e, плюс служебные (расстояния, токены, ссылки).
latency_ms здесь — только вызов LLM: поиск идёт в памяти, эмбеддинги
вопросов кэшируются.
"""

import argparse
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

from corp_ed.domain.context import select_context
from corp_ed.domain.fusion import rrf_merge
from corp_ed.llm.types import Message
from corp_ed.prompts.faq import (
    NOT_FOUND_ANSWER,
    PROMPT_VERSION,
    build_faq_messages,
    build_general_messages,
    ensure_general_prefix,
    is_not_found,
    normalize_citations,
)
from eval.bench import FUSION_CANDIDATES, bm25_rankings, vector_rankings
from eval.corpus import BenchChunk, ChunkingConfig, chunk_corpus, load_corpus
from eval.datasets import EvalItem, load_dataset, select_split
from eval.metrics import percentile
from eval.results import RESULTS_DIR, append_summary, results_path, write_csv
from eval.run_eval import is_answered, looks_like_refusal, summarize_e2e
from eval.yandex import (
    DEFAULT_API,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_LLM,
    DEFAULT_MAX_DISTANCE,
    Completion,
    default_embedding_dim,
)

NotFoundMode = Literal["strict", "general"]
Retriever = Literal["vector", "hybrid"]

_CITATION = re.compile(r"\[(\d+)\]")
_SECTION_CITATION = re.compile(r"\[\d+(?:\.\d+)+\.?\]")
"""[2.2], [6.1.1] — номер пункта документа вместо номера выдержки."""


@dataclass(frozen=True)
class OfflineMatch:
    """Найденный чанк в форме, которую ждут select_context и промпт."""

    content: str
    title: str
    heading_path: list[str] = field(default_factory=list)
    distance: float | None = 0.0
    """Косинусное расстояние; None — чанк нашёл только BM25 (гибрид)."""

    @classmethod
    def from_chunk(cls, chunk: BenchChunk, distance: float | None) -> "OfflineMatch":
        return cls(
            content=chunk.llm_text,
            title=chunk.material,
            heading_path=chunk.heading_path,
            distance=distance,
        )


def relevant_matches(
    matches: Sequence[OfflineMatch], max_distance: float
) -> list[OfflineMatch]:
    """Как faq_service: отбросить всё дальше порога, порядок сохранить."""
    return [
        match
        for match in matches
        if match.distance is not None and match.distance <= max_distance
    ]


def gate_by_best_distance(
    matches: Sequence[OfflineMatch], best_distance: float | None, max_distance: float
) -> list[OfflineMatch]:
    """M1: после RRF у чанков из BM25 расстояния нет, порог — по лучшему
    векторному кандидату: прошёл — берём всю выдачу, нет — отказ."""
    if best_distance is None or best_distance > max_distance:
        return []
    return list(matches)


def retrieve(
    chunks: Sequence[BenchChunk],
    questions: Sequence[str],
    *,
    retriever: Retriever,
    limit: int,
    weights: Sequence[float] = (1.0, 0.5),
    rrf_k: int = 60,
    workers: int = 4,
    embedding_model: str = "text-search",
    embedding_dim: int | None = None,
) -> list[tuple[list[OfflineMatch], float | None]]:
    """Для каждого вопроса: top-limit чанков и лучшее векторное расстояние."""
    depth = limit if retriever == "vector" else max(limit, FUSION_CANDIDATES)
    vec_rank, vec_dist = vector_rankings(
        chunks, questions, depth, workers, embedding_model, embedding_dim
    )
    if retriever == "vector":
        return [
            (
                [
                    OfflineMatch.from_chunk(chunks[i], d)
                    for i, d in zip(ranking, dists, strict=True)
                ],
                dists[0] if dists else None,
            )
            for ranking, dists in zip(vec_rank, vec_dist, strict=True)
        ]
    text_rank = bm25_rankings(chunks, questions, depth)
    results: list[tuple[list[OfflineMatch], float | None]] = []
    for v_rank, v_dist, t_rank in zip(vec_rank, vec_dist, text_rank, strict=True):
        distance_of = dict(zip(v_rank, v_dist, strict=True))
        merged = rrf_merge([v_rank, t_rank], list(weights), k=rrf_k)[:limit]
        results.append(
            (
                [
                    OfflineMatch.from_chunk(chunks[i], distance_of.get(i))
                    for i, _ in merged
                ],
                v_dist[0] if v_dist else None,
            )
        )
    return results


def citation_numbers(answer: str) -> list[int]:
    return [int(number) for number in _CITATION.findall(answer)]


def invalid_citations(answer: str, n_sources: int) -> list[int]:
    """Ссылки [n] на выдержки, которых модель не видела."""
    return [n for n in citation_numbers(answer) if not 1 <= n <= n_sources]


def section_citations(answer: str) -> int:
    """Сколько раз модель поставила в скобки номер пункта, а не выдержки."""
    return len(_SECTION_CITATION.findall(answer))


def _sources_json(matches: Sequence[OfflineMatch]) -> str:
    return json.dumps(
        [
            {
                "material": m.title,
                "heading_path": m.heading_path,
                "content": m.content,
                "distance": None if m.distance is None else round(m.distance, 4),
            }
            for m in matches
        ],
        ensure_ascii=False,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.offline_e2e",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--split", default="", help="только вопросы split (dev/test)")
    parser.add_argument("--model", default=DEFAULT_LLM)
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="0 — воспроизводимо; при 0.3 (как сейчас у бэкенда) ответы гуляют",
    )
    parser.add_argument(
        "--api",
        choices=("native", "openai"),
        default=DEFAULT_API,
        help="openai — все модели каталога (бэкенд после BH-15); "
        "native — только YandexGPT (бэкенд до переезда)",
    )
    parser.add_argument(
        "--max-tokens", type=int, default=1000, help="лимит выхода, с рассуждением"
    )
    parser.add_argument("--embedding-model", default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument(
        "--embedding-dim", type=int, default=None, help="по умолчанию 768 для v2"
    )
    parser.add_argument("--limit", type=int, default=5, help="faq_limit (E4)")
    parser.add_argument(
        "--max-distance",
        type=float,
        default=DEFAULT_MAX_DISTANCE,
        help="0.51 — для v2-768 (предварительно); для text-search было 0.65",
    )
    parser.add_argument("--context-tokens", type=int, default=3000)
    parser.add_argument(
        "--not-found",
        choices=("strict", "general"),
        default="general",
        help="Р1 (решено 25.09): general — общий ответ с пометкой «не из документов "
        "компании», и когда выдержек нет, и когда модель по ним отказала; "
        "strict — только фраза отказа",
    )
    parser.add_argument("--retriever", choices=("vector", "hybrid"), default="vector")
    parser.add_argument(
        "--weights", default="1.0,0.5", help="hybrid: вектор,полнотекст (RRF)"
    )
    parser.add_argument("--rrf-k", type=int, default=60)
    parser.add_argument("--chunk-tokens", type=int, default=400)
    parser.add_argument("--overlap-tokens", type=int, default=50)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--config", default="", help="имя прогона (иначе — из флагов)")
    parser.add_argument("--out", type=Path, default=RESULTS_DIR)
    parser.add_argument("--notes", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    from eval.yandex import YandexClient

    chunking = ChunkingConfig(
        chunk_tokens=args.chunk_tokens, overlap_tokens=args.overlap_tokens
    )
    mode: NotFoundMode = args.not_found
    retriever: Retriever = args.retriever
    embedding_dim = args.embedding_dim or default_embedding_dim(args.embedding_model)
    embedder = (
        ""
        if args.embedding_model == "text-search"
        else f"-{args.embedding_model}" + (f"-{embedding_dim}" if embedding_dim else "")
    )
    config = args.config or (
        f"offline-{args.model}-{chunking.name}-{retriever}{embedder}"
        f"-k{args.limit}-d{args.max_distance}-{mode}"
    )

    chunks = chunk_corpus(load_corpus(args.corpus), chunking)
    items: list[EvalItem] = load_dataset(args.dataset)
    # Без --split holdout золотого набора не берём (select_split, задача 2.1).
    items = select_split(items, args.split)
    print(f"Чанков: {len(chunks)} ({chunking.name}), вопросов: {len(items)}")

    retrieved = retrieve(
        chunks,
        [item.question for item in items],
        retriever=retriever,
        limit=args.limit,
        weights=[float(w) for w in args.weights.split(",")],
        rrf_k=args.rrf_k,
        workers=args.workers,
        embedding_model=args.embedding_model,
        embedding_dim=embedding_dim,
    )
    client = YandexClient.from_env()

    def ask(messages: list[Message]) -> Completion:
        return client.complete(
            messages,
            model=args.model,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            api=args.api,
        )

    rows: list[dict[str, object]] = []
    for number, (item, (found, best)) in enumerate(
        zip(items, retrieved, strict=True), start=1
    ):
        relevant = (
            relevant_matches(found, args.max_distance)
            if retriever == "vector"
            else gate_by_best_distance(found, best, args.max_distance)
        )
        selected = select_context(relevant, args.context_tokens)

        calls: list[Completion] = []
        raw = answer = NOT_FOUND_ANSWER
        general = False
        if selected:
            calls.append(ask(build_faq_messages(item.question, selected)))
            raw = calls[-1].text.strip()
            answer = normalize_citations(raw, selected)
        # Р1 (решено 25.09): ответа в документах нет — ни одна выдержка не
        # прошла порог или модель по выдержкам ответила отказом — общий ответ
        # со строгой пометкой, что он не из документов компании.
        if mode == "general" and (not selected or is_not_found(raw)):
            calls.append(ask(build_general_messages(item.question)))
            answer = ensure_general_prefix(calls[-1].text.strip())
            general = True

        latency = sum(c.latency_ms for c in calls)
        tokens_in = sum(c.input_tokens for c in calls)
        tokens_out = sum(c.output_tokens for c in calls)
        reasoning = sum(c.reasoning_tokens for c in calls)
        finish = calls[-1].finish_reason if calls else ""

        answered = is_answered(answer, answer_given=bool(selected))
        rows.append(
            {
                "id": item.id,
                "type": item.type,
                "in_corpus": item.in_corpus,
                "question": item.question,
                "expected_answer": item.expected_answer,
                "expected_material": item.expected_material,
                "answer": answer,
                "answered": answered,
                "answer_given": bool(selected),
                "general_answer": general,
                "general_after_refusal": general and bool(selected),
                "refusal_paraphrase": looks_like_refusal(answer),
                "n_sources": len(selected),
                "sources": _sources_json(selected),
                "latency_ms": round(latency),
                "extra": "",
                "correct": "",
                "faithful": "",
                "comment": "",
                "best_distance": "" if best is None else round(best, 4),
                "n_found": len(found),
                "n_relevant": len(relevant),
                "citations": len(citation_numbers(answer)),
                # Сколько раз ошиблась модель — до normalize_citations: после
                # неё номеров вне 1..k и [2.2] в ответе уже нет.
                "invalid_citations": len(invalid_citations(raw, len(selected))),
                "section_citations": section_citations(raw),
                "llm_calls": len(calls),
                "input_tokens": tokens_in,
                "output_tokens": tokens_out,
                "reasoning_tokens": reasoning,
                "finish_reason": finish,
            }
        )
        status = "ответил" if answered else "отказал"
        print(
            f"[{number}/{len(items)}] {item.id}: {status}, "
            f"выдержек {len(selected)}, {latency:.0f} мс"
        )

    summary = summarize_e2e(rows)
    # Без вызова LLM latency_ms = 0: в p50/p95 генерации такие строки не идут.
    llm_latencies = [float(str(r["latency_ms"])) for r in rows if r["input_tokens"]]
    summary["latency_p50_ms"] = percentile(llm_latencies, 50) if llm_latencies else 0.0
    summary["latency_p95_ms"] = percentile(llm_latencies, 95) if llm_latencies else 0.0
    answered_rows = [row for row in rows if row["answered"]]
    extra = {
        "uncited_answers": sum(1 for row in answered_rows if not row["citations"]),
        "invalid_citations": sum(int(str(row["invalid_citations"])) for row in rows),
        "section_answers": sum(1 for row in rows if row["section_citations"]),
        "input_tokens": sum(int(str(row["input_tokens"])) for row in rows),
        "output_tokens": sum(int(str(row["output_tokens"])) for row in rows),
        "llm_calls": sum(int(str(row["llm_calls"])) for row in rows),
        "general_answers": sum(1 for row in rows if row["general_answer"]),
        "general_after_refusal": sum(1 for row in rows if row["general_after_refusal"]),
    }
    path = results_path(args.out, config, "e2e", date.today())
    write_csv(path, rows)
    append_summary(
        args.out,
        {
            **summary,
            "date": date.today().isoformat(),
            "config": config,
            "mode": "offline-e2e",
            "dataset": args.dataset.name + (f":{args.split}" if args.split else ""),
            "results_file": path.name,
            "notes": args.notes
            or f"{PROMPT_VERSION}; {chunking.name}; ctx={args.context_tokens}",
        },
    )
    print(
        f"\nF1 отказа {float(str(summary['f1'])):.3f} "
        f"(precision {float(str(summary['precision'])):.3f}, "
        f"recall {float(str(summary['recall'])):.3f}); "
        f"отказов своими словами: {summary['refusal_paraphrases']}"
    )
    print(
        f"Ответов без ссылок [n]: {extra['uncited_answers']} из {len(answered_rows)}; "
        f"ссылок на несуществующие выдержки: {extra['invalid_citations']}; "
        f"ответов с номерами пунктов вида [2.2]: {extra['section_answers']}"
    )
    print(
        f"LLM p50/p95: {float(str(summary['latency_p50_ms'])):.0f} / "
        f"{float(str(summary['latency_p95_ms'])):.0f} мс; "
        f"вызовов {extra['llm_calls']}, токенов вход {extra['input_tokens']}, "
        f"выход {extra['output_tokens']}"
    )
    if mode == "general":
        print(
            f"Общих ответов с пометкой: {extra['general_answers']}, из них после "
            f"отказа по выдержкам: {extra['general_after_refusal']}"
        )
    print(f"Результаты: {path}")
    print("Разметка correct и подсчёт: python -m eval.run_eval score --results …")
    return 0


if __name__ == "__main__":
    sys.exit(main())
