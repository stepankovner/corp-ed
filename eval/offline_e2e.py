"""Офлайн-e2e: конвейер /faq/ask без бэкенда (предпросмотр E4, E5 и Р1).

    python -m eval.offline_e2e --corpus corpus/ --dataset eval/golden.csv \\
        --model yandexgpt-lite --limit 5 --max-distance 0.6

Официальные числа — через run_eval e2e (HTTP API бэкенда). Этот стенд
нужен, пока API нет, и для перебора того, что на бэкенде меняется только
переменными окружения: faq_limit (E4), модель (E5), порог (A8), режим
«ответа нет» (Р1).

Шаги повторяют бэкенд (docs/backend-handoff.md, BH-3 и BH-7):
1. top-limit чанков: по косинусному расстоянию (text-search-query → doc)
   или гибрид вектор + BM25 через RRF (--retriever hybrid, предпросмотр M1);
2. порог: для вектора — каждый чанк дальше max_distance отбрасывается
   (как faq_service сейчас); для гибрида — по лучшему векторному
   кандидату (контракт M1). Не осталось ни одного — LLM не вызывается:
   фраза отказа (strict) или общий ответ с пометкой (general, Р1);
3. select_context — бюджет контекста в токенах;
4. build_faq_messages (промпт PROMPT_VERSION) → YandexGPT →
   normalize_citations ([4.2] → номер выдержки), как должен делать бэкенд.

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
from corp_ed.prompts.faq import (
    NOT_FOUND_ANSWER,
    PROMPT_VERSION,
    build_faq_messages,
    build_general_messages,
    ensure_general_prefix,
    normalize_citations,
)
from eval.bench import FUSION_CANDIDATES, bm25_rankings, vector_rankings
from eval.corpus import BenchChunk, ChunkingConfig, chunk_corpus, load_corpus
from eval.datasets import EvalItem, load_dataset
from eval.metrics import percentile
from eval.results import RESULTS_DIR, append_summary, results_path, write_csv
from eval.run_eval import is_answered, looks_like_refusal, summarize_e2e

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
) -> list[tuple[list[OfflineMatch], float | None]]:
    """Для каждого вопроса: top-limit чанков и лучшее векторное расстояние."""
    depth = limit if retriever == "vector" else max(limit, FUSION_CANDIDATES)
    vec_rank, vec_dist = vector_rankings(
        chunks, questions, depth, workers, embedding_model
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
    parser.add_argument("--model", default="yandexgpt-lite")
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="0 — воспроизводимо; при 0.3 (как сейчас у бэкенда) ответы гуляют",
    )
    parser.add_argument(
        "--api",
        choices=("native", "openai"),
        default="native",
        help="native — как бэкенд (только YandexGPT); openai — все модели каталога",
    )
    parser.add_argument(
        "--max-tokens", type=int, default=1000, help="лимит выхода, с рассуждением"
    )
    parser.add_argument("--embedding-model", default="text-search")
    parser.add_argument("--limit", type=int, default=5, help="faq_limit (E4)")
    parser.add_argument("--max-distance", type=float, default=0.6)
    parser.add_argument("--context-tokens", type=int, default=3000)
    parser.add_argument(
        "--not-found",
        choices=("strict", "general"),
        default="strict",
        help="Р1: strict — только фраза отказа; general — общий ответ с пометкой",
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
    config = args.config or (
        f"offline-{args.model}-{chunking.name}-{retriever}"
        f"-k{args.limit}-d{args.max_distance}-{mode}"
    )

    chunks = chunk_corpus(load_corpus(args.corpus), chunking)
    items: list[EvalItem] = load_dataset(args.dataset)
    if args.split:
        items = [item for item in items if item.split == args.split]
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
    )
    client = YandexClient.from_env()

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

        messages = None
        if selected:
            messages = build_faq_messages(item.question, selected)
        elif mode == "general":
            messages = build_general_messages(item.question)

        raw = answer = NOT_FOUND_ANSWER
        latency, tokens_in, tokens_out, reasoning, finish = 0.0, 0, 0, 0, ""
        if messages is not None:
            completion = client.complete(
                messages,
                model=args.model,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
                api=args.api,
            )
            raw = completion.text.strip()
            answer = (
                normalize_citations(raw, selected)
                if selected
                else ensure_general_prefix(raw)
            )
            latency = completion.latency_ms
            tokens_in, tokens_out = completion.input_tokens, completion.output_tokens
            reasoning, finish = completion.reasoning_tokens, completion.finish_reason

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
        "llm_calls": len(llm_latencies),
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
    print(f"Результаты: {path}")
    print("Разметка correct и подсчёт: python -m eval.run_eval score --results …")
    return 0


if __name__ == "__main__":
    sys.exit(main())
