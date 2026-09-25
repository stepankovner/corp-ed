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
   --multi-query N (M6): модель переписывает вопрос N способами
   (prompts.multi_query, mq-v1), ищем по всем и сливаем RRF
   (fuse_query_rankings); порог тогда — по лучшему расстоянию исходного
   вопроса;
2. порог: для вектора — каждый чанк дальше max_distance отбрасывается
   (как faq_service сейчас); для гибрида — по лучшему векторному
   кандидату (контракт M1). Не осталось ни одного — ответ по выдержкам не
   вызывается: общий ответ с пометкой (general, Р1 — по умолчанию) или
   фраза отказа (strict);
3. select_context — бюджет контекста в токенах; --context sections|window —
   small-to-big (M2, select_sections): на место найденного чанка встаёт
   его секция целиком или окно соседей. Колонки context_kinds,
   context_tokens и evidence_in_context (цитата эталона попала в
   контекст) считаются и без LLM: --dry-run — бесплатный замер;
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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

from corp_ed.domain.context import (
    ContextBlock,
    Section,
    select_context,
    select_sections,
)
from corp_ed.domain.fusion import rrf_merge
from corp_ed.domain.query import fuse_query_rankings
from corp_ed.domain.tokens import count_tokens
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
from eval.corpus import (
    BenchChunk,
    ChunkingConfig,
    chunk_corpus,
    load_corpus,
    section_corpus,
)
from eval.datasets import EvalItem, load_dataset, select_split
from eval.metrics import percentile
from eval.multi_query import Paraphrased, paraphrase_questions
from eval.relevance import EVIDENCE_MIN_COVERAGE, evidence_coverage
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
ContextMode = Literal["chunks", "sections", "window"]

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
    section_id: str = ""
    """Ключ секции в section_corpus — для select_sections (M2)."""

    @classmethod
    def from_chunk(cls, chunk: BenchChunk, distance: float | None) -> "OfflineMatch":
        return cls(
            content=chunk.llm_text,
            title=chunk.material,
            heading_path=chunk.heading_path,
            distance=distance,
            section_id=chunk.section_id,
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


def build_context(
    matches: Sequence[OfflineMatch],
    *,
    mode: ContextMode,
    sections: Mapping[str, Section],
    max_tokens: int,
    neighbours: int,
) -> list[ContextBlock[OfflineMatch]]:
    """Выдержки для промпта: чанки (как бэкенд сейчас) или small-to-big (M2).

    sections — секция целиком, если влезает, иначе окно соседей, иначе
    чанк; window — то же без таблицы sections: только окно или чанк
    (BH-13, вариант без новой таблицы).
    """
    if mode == "chunks":
        return [
            ContextBlock(
                content=match.content,
                title=match.title,
                heading_path=match.heading_path,
                match=match,
                kind="chunk",
            )
            for match in select_context(matches, max_tokens)
        ]
    known: Mapping[str, Section | str] = (
        sections
        if mode == "sections"
        else {
            key: Section(content=None, chunks=section.chunks)
            for key, section in sections.items()
        }
    )
    return select_sections(matches, known, max_tokens, neighbours=neighbours)


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
    paraphrases: Sequence[Sequence[str]] | None = None,
    paraphrase_weight: float = 1.0,
) -> list[tuple[list[OfflineMatch], float | None]]:
    """Для каждого вопроса: top-limit чанков и лучшее векторное расстояние.

    paraphrases (M6) — переформулировки каждого вопроса: выдачи по ним
    сливаются с выдачей по вопросу (fuse_query_rankings). Расстояние у
    чанка — до ИСХОДНОГО вопроса; у найденного только переформулировкой
    его нет (None), поэтому с multi-query порог — по лучшему расстоянию
    исходного вопроса, как в гибриде.
    """
    groups = [list(group) for group in paraphrases] if paraphrases else []
    flat = [*questions, *(query for group in groups for query in group)]
    depth = (
        limit
        if retriever == "vector" and not any(groups)
        else max(limit, FUSION_CANDIDATES)
    )
    ranked = rank_queries(
        chunks,
        flat,
        retriever=retriever,
        depth=depth,
        weights=weights,
        rrf_k=rrf_k,
        workers=workers,
        embedding_model=embedding_model,
        embedding_dim=embedding_dim,
    )
    results: list[tuple[list[OfflineMatch], float | None]] = []
    offset = len(questions)
    for index in range(len(questions)):
        ranking, distance_of = ranked[index]
        extra = [
            ranked[offset + j][0] for j in range(len(groups[index]) if groups else 0)
        ]
        offset += len(extra)
        if extra:
            ranking = fuse_query_rankings(
                ranking, extra, paraphrase_weight=paraphrase_weight, k=rrf_k
            )
        results.append(
            (
                [
                    OfflineMatch.from_chunk(chunks[i], distance_of.get(i))
                    for i in ranking[:limit]
                ],
                min(distance_of.values(), default=None),
            )
        )
    return results


def rank_queries(
    chunks: Sequence[BenchChunk],
    queries: Sequence[str],
    *,
    retriever: Retriever,
    depth: int,
    weights: Sequence[float],
    rrf_k: int,
    workers: int,
    embedding_model: str,
    embedding_dim: int | None,
) -> list[tuple[list[int], dict[int, float]]]:
    """Для каждого запроса: индексы чанков по убыванию и расстояния векторной
    ветки (у гибрида — после RRF вектора и BM25; расстояния только у тех,
    кого нашёл вектор)."""
    vec_rank, vec_dist = vector_rankings(
        chunks, queries, depth, workers, embedding_model, embedding_dim
    )
    if retriever == "vector":
        return [
            (ranking, dict(zip(ranking, dists, strict=True)))
            for ranking, dists in zip(vec_rank, vec_dist, strict=True)
        ]
    text_rank = bm25_rankings(chunks, queries, depth)
    return [
        (
            [i for i, _ in rrf_merge([v_rank, t_rank], list(weights), k=rrf_k)],
            dict(zip(v_rank, v_dist, strict=True)),
        )
        for v_rank, v_dist, t_rank in zip(vec_rank, vec_dist, text_rank, strict=True)
    ]


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
        "--context",
        choices=("chunks", "sections", "window"),
        default="chunks",
        help="M2: chunks — как бэкенд сейчас; sections — секция целиком, если "
        "влезает, иначе окно соседей, иначе чанк; window — без таблицы "
        "sections: чанк ± --neighbours по секции",
    )
    parser.add_argument(
        "--neighbours", type=int, default=1, help="M2: соседей с каждой стороны"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="без вызовов LLM: только поиск и контекст (бесплатно)",
    )
    parser.add_argument(
        "--multi-query",
        type=int,
        default=0,
        help="M6: сколько переформулировок вопроса искать вместе с ним "
        "(0 — выключено; один вызов LLM на вопрос, кэшируется)",
    )
    parser.add_argument(
        "--mq-weight", type=float, default=1.0, help="M6: вес переформулировки в RRF"
    )
    parser.add_argument("--mq-model", default=DEFAULT_LLM, help="M6: модель")
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
    context_mode: ContextMode = args.context
    embedding_dim = args.embedding_dim or default_embedding_dim(args.embedding_model)
    embedder = (
        ""
        if args.embedding_model == "text-search"
        else f"-{args.embedding_model}" + (f"-{embedding_dim}" if embedding_dim else "")
    )
    context_name = {
        "chunks": "",
        "sections": "-sections",
        "window": f"-window{args.neighbours}",
    }[context_mode]
    mq_name = (
        f"-mq{args.multi_query}"
        + (f"-w{args.mq_weight}" if args.mq_weight != 1.0 else "")
        if args.multi_query
        else ""
    )
    config = args.config or (
        f"offline-{args.model}-{chunking.name}-{retriever}{embedder}"
        f"-k{args.limit}-d{args.max_distance}-{mode}{context_name}{mq_name}"
        + ("-dry" if args.dry_run else "")
    )

    documents = load_corpus(args.corpus)
    chunks = chunk_corpus(documents, chunking)
    sections = section_corpus(documents, chunking) if context_mode != "chunks" else {}
    items: list[EvalItem] = load_dataset(args.dataset)
    # Без --split holdout золотого набора не берём (select_split, задача 2.1).
    items = select_split(items, args.split)
    print(f"Чанков: {len(chunks)} ({chunking.name}), вопросов: {len(items)}")

    # M6: переформулировки — один вызов LLM на вопрос, с кэшем; идёт и при
    # --dry-run (это не ответ, а поиск), повторно — бесплатно.
    paraphrased: list[Paraphrased] = [Paraphrased() for _ in items]
    if args.multi_query:
        paraphrased = paraphrase_questions(
            YandexClient.from_env(),
            [item.question for item in items],
            count=args.multi_query,
            model=args.mq_model,
            api=args.api,
        )
        print(
            f"Переформулировок: {sum(len(p.queries) for p in paraphrased)} "
            f"на {len(items)} вопросов, из кэша {sum(p.cached for p in paraphrased)}"
        )

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
        paraphrases=[p.queries for p in paraphrased] if args.multi_query else None,
        paraphrase_weight=args.mq_weight,
    )
    # --dry-run: LLM не вызывается — поиск и контекст замеряются бесплатно.
    client = None if args.dry_run else YandexClient.from_env()

    def ask(messages: list[Message]) -> Completion:
        assert client is not None
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
        # С multi-query у чанка из переформулировки нет расстояния до вопроса,
        # поэтому порог — по лучшему расстоянию исходного вопроса (как M1).
        relevant = (
            relevant_matches(found, args.max_distance)
            if retriever == "vector" and not args.multi_query
            else gate_by_best_distance(found, best, args.max_distance)
        )
        selected = build_context(
            relevant,
            mode=context_mode,
            sections=sections,
            max_tokens=args.context_tokens,
            neighbours=args.neighbours,
        )
        context_text = "\n".join(block.content for block in selected)

        calls: list[Completion] = []
        raw = answer = NOT_FOUND_ANSWER
        general = False
        if selected and not args.dry_run:
            calls.append(ask(build_faq_messages(item.question, selected)))
            raw = calls[-1].text.strip()
            answer = normalize_citations(raw, selected)
        # Р1 (решено 25.09): ответа в документах нет — ни одна выдержка не
        # прошла порог или модель по выдержкам ответила отказом — общий ответ
        # со строгой пометкой, что он не из документов компании.
        if (
            mode == "general"
            and not args.dry_run
            and (not selected or is_not_found(raw))
        ):
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
                "sources": _sources_json([block.match for block in selected]),
                # M2: из чего собран контекст и сколько он стоит в токенах.
                "context_kinds": ",".join(block.kind for block in selected),
                "context_tokens": count_tokens(context_text) if selected else 0,
                # Цитата эталона в контексте, который увидела модель (только у
                # вопросов с evidence — золотой набор). Бесплатная метрика M2:
                # не «нашли чанк», а «модели показали текст с ответом».
                "evidence_in_context": (
                    ""
                    if not item.evidence
                    else evidence_coverage(item.evidence, context_text)
                    >= EVIDENCE_MIN_COVERAGE
                ),
                # M6: переформулировки и их цена (отдельно от вызова ответа).
                "mq_queries": " | ".join(paraphrased[number - 1].queries),
                "mq_input_tokens": paraphrased[number - 1].input_tokens,
                "mq_output_tokens": paraphrased[number - 1].output_tokens,
                "mq_latency_ms": round(paraphrased[number - 1].latency_ms),
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
        "context_tokens": sum(int(str(row["context_tokens"])) for row in rows),
        "whole_sections": sum(
            str(row["context_kinds"]).split(",").count("section") for row in rows
        ),
        "windows": sum(
            str(row["context_kinds"]).split(",").count("window") for row in rows
        ),
    }
    with_evidence = [row for row in rows if row["evidence_in_context"] != ""]
    extra["with_evidence"] = len(with_evidence)
    extra["evidence_in_context"] = sum(
        1 for row in with_evidence if row["evidence_in_context"]
    )
    extra["mq_input_tokens"] = sum(int(str(row["mq_input_tokens"])) for row in rows)
    extra["mq_output_tokens"] = sum(int(str(row["mq_output_tokens"])) for row in rows)
    if args.multi_query:
        print(
            f"Переформулировки (M6): токенов вход {extra['mq_input_tokens']}, "
            f"выход {extra['mq_output_tokens']}; из кэша "
            f"{sum(p.cached for p in paraphrased)} из {len(paraphrased)}"
        )
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
            or f"{PROMPT_VERSION}; {chunking.name}; ctx={args.context_tokens}; "
            f"context={context_name.lstrip('-') or 'chunks'}",
        },
    )
    print(
        f"\nКонтекст ({context_mode}): токенов {extra['context_tokens']}, "
        f"секций целиком {extra['whole_sections']}, окон {extra['windows']}; "
        f"цитата эталона в контексте: {extra['evidence_in_context']} "
        f"из {extra['with_evidence']}"
    )
    if args.dry_run:
        print("LLM не вызывался (--dry-run): F1, ссылки и задержки не считались")
    else:
        print(
            f"F1 отказа {float(str(summary['f1'])):.3f} "
            f"(precision {float(str(summary['precision'])):.3f}, "
            f"recall {float(str(summary['recall'])):.3f}); "
            f"отказов своими словами: {summary['refusal_paraphrases']}"
        )
        print(
            f"Ответов без ссылок [n]: {extra['uncited_answers']} "
            f"из {len(answered_rows)}; "
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
                f"Общих ответов с пометкой: {extra['general_answers']}, из них "
                f"после отказа по выдержкам: {extra['general_after_refusal']}"
            )
    print(f"Результаты: {path}")
    print("Разметка correct и подсчёт: python -m eval.run_eval score --results …")
    return 0


if __name__ == "__main__":
    sys.exit(main())
