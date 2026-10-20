"""Офлайн-e2e: конвейер /faq/ask без бэкенда (предпросмотр E4, E5 и Р1).

    python -m eval.offline_e2e --corpus corpus/ --dataset eval/private/golden.csv \\
        --limit 5

По умолчанию — конфигурация, выбранная по задаче 1 (25.09): Alice AI LLM
Flash через OpenAI-совместимый API, text-embeddings-v2 с размерностью 768,
порог 0.59 (30.09; до этого 0.51). Старая: --api native --model yandexgpt-lite
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
4. build_faq_messages (промпт SEARCH_PROMPT_VERSION, второй шаг поиска) → LLM →
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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

from corp_ed.domain.agent import drop_citations
from corp_ed.domain.context import (
    ContextBlock,
    Section,
    select_context,
    select_sections,
)
from corp_ed.domain.decision import parse_decision
from corp_ed.domain.directory import (
    covers,
    format_entries,
    lookup,
    parse_directory,
    requested_fields,
)
from corp_ed.domain.fusion import rrf_merge
from corp_ed.domain.query import fuse_query_rankings
from corp_ed.domain.rerank import RerankOrder, rerank_allowed
from corp_ed.domain.threshold import relevance_limit
from corp_ed.domain.tokens import count_tokens
from corp_ed.llm.types import Message
from corp_ed.prompts.decision import (
    DECISION_PROMPT_VERSION,
    build_decision_messages,
    decision_response_format,
    with_other_subject_note,
)
from corp_ed.prompts.faq import (
    NOT_FOUND_ANSWER,
    SEARCH_PROMPT_VERSION,
    SMALL_TALK_PROMPT_VERSION,
    build_faq_messages,
    build_general_messages,
    ensure_general_prefix,
    is_not_found,
    normalize_citations,
    search_request,
    small_talk_reply,
    split_general_answer,
)
from eval.agent_loop import AgentRun, run_agent
from eval.bench import (
    FUSION_CANDIDATES,
    add_rerank_arguments,
    bm25_rankings,
    rerank_text,
    vector_rankings,
)
from eval.corpus import (
    BenchChunk,
    ChunkingConfig,
    chunk_corpus,
    load_corpus,
    section_corpus,
)
from eval.datasets import EvalItem, load_dataset, select_split
from eval.decision_loop import RECHECK_MODES, DecidedRun, run_decided
from eval.doc_context import add_doc_context_arguments, with_doc_context
from eval.metrics import percentile
from eval.multi_query import Paraphrased, paraphrase_questions
from eval.relevance import EVIDENCE_MIN_COVERAGE, evidence_coverage
from eval.rerank import (
    CachedReranker,
    config_suffix,
    make_reranker,
    rerank_candidates,
    rerank_rule_suffix,
)
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
SEARCH_MORE_LIMIT = 3
"""faq-v2.9: сколько новых фрагментов второго шага поиска добавить к прежним."""


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
    matches: Sequence[OfflineMatch],
    max_distance: float,
    *,
    gate_distance: float | None = None,
    near_margin: float = 0.0,
    nearest: float | None = None,
) -> list[OfflineMatch]:
    """Как faq_service: отбросить всё дальше порога, порядок сохранить.

    gate_distance (BH-37, замер 04.10): если ближайшая выдержка дальше
    max_distance, но не дальше gate_distance, в модель идут она и выдержки
    не дальше неё + near_margin (`domain.threshold.relevance_limit`).

    nearest — ближайший по вектору фрагмент всей выдачи, как found.nearest
    продукта. После реранкера его может не быть среди matches, и порог по
    ближайшему из них был бы другим. Без него — ближайший из matches.
    """
    if nearest is None:
        known = [m.distance for m in matches if m.distance is not None]
        nearest = min(known) if known else None
    limit = relevance_limit(nearest, max_distance, gate_distance, near_margin)
    if limit is None:
        return []
    return [
        match
        for match in matches
        if match.distance is not None and match.distance <= limit
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
    reranker: CachedReranker | None = None,
    rerank_depth: int = 30,
    rerank_max_distance: float | None = None,
    rerank_kind: str = "embed",
    gate_distance: float | None = None,
    near_margin: float = 0.0,
    rerank_order: RerankOrder = "score",
    rerank_max_words: int | None = None,
) -> list[tuple[list[OfflineMatch], float | None]]:
    """Для каждого вопроса: top-limit чанков и лучшее векторное расстояние.

    paraphrases (M6) — переформулировки каждого вопроса: выдачи по ним
    сливаются с выдачей по вопросу (fuse_query_rankings). Расстояние у
    чанка — до ИСХОДНОГО вопроса; у найденного только переформулировкой
    его нет (None), поэтому с multi-query порог — по лучшему расстоянию
    исходного вопроса, как в гибриде.

    reranker (M3) — первые rerank_depth векторных кандидатов, прошедших
    rerank_max_distance, пересортировываются кросс-энкодером; в выдачу —
    первые limit. Порог по вектору остаётся (relevant_matches).

    gate_distance (BH-37) — порог реранкера у каждого вопроса свой, как в
    faq_service: `relevance_limit` по ближайшему фрагменту; ответа по
    документам нет — реранкер не зовём.

    rerank_order, rerank_max_words — кандидаты починки длинных вопросов
    (`domain.rerank.order_passed`, `rerank_allowed`; 04.10).
    """
    if (
        reranker is not None
        and gate_distance is not None
        and rerank_max_distance is None
    ):
        raise ValueError("с gate_distance нужен rerank_max_distance — порог продукта")
    groups = [list(group) for group in paraphrases] if paraphrases else []
    flat = [*questions, *(query for group in groups for query in group)]
    depth = (
        limit
        if retriever == "vector" and not any(groups)
        else max(limit, FUSION_CANDIDATES)
    )
    if reranker is not None:
        depth = max(depth, rerank_depth)
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
        nearest = min(distance_of.values(), default=None)
        cutoff = rerank_max_distance
        if gate_distance is not None and rerank_max_distance is not None:
            cutoff = relevance_limit(
                nearest, rerank_max_distance, gate_distance, near_margin
            )
        # С gate ответа по документам нет — продукт реранкер не зовёт; без
        # gate None — оценить всех первых rerank_depth (как было).
        question = questions[index]
        if (
            reranker is not None
            and (gate_distance is None or cutoff is not None)
            and rerank_allowed(question, rerank_max_words)
        ):
            ranking = rerank_candidates(
                ranking,
                distance_of,
                lambda idx, q=question: reranker.score(
                    q, [rerank_text(chunks[i], rerank_kind) for i in idx]
                ),
                depth=rerank_depth,
                max_distance=cutoff,
                order=rerank_order,
            )
        results.append(
            (
                [
                    OfflineMatch.from_chunk(chunks[i], distance_of.get(i))
                    for i in ranking[:limit]
                ],
                nearest,
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


def _directory_search(
    title: str, markdown: str
) -> Callable[[str, str], tuple[list[ContextBlock[OfflineMatch]], bool]]:
    """Поиск по справочнику оргструктуры (decision-v4): записи — одной
    выдержкой (как блок контекста, чтобы источники ответа видели документ)."""
    entries = parse_directory(markdown)
    print(f"Справочник: «{title}», записей {len(entries)}")

    def search(
        role: str, missing: str
    ) -> tuple[list[ContextBlock[OfflineMatch]], bool]:
        found = lookup(entries, role)
        if not found:
            return [], False
        complete = covers(found, requested_fields(missing))
        content = format_entries(found)
        match = OfflineMatch(
            content=content, title=title, heading_path=["Справочник"], distance=0.0
        )
        block = ContextBlock(
            content=content,
            title=title,
            heading_path=["Справочник"],
            match=match,
            kind="chunk",
        )
        return [block], complete

    return search


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
        help="0 — как в продукте (RAG_FAQ_TEMPERATURE); текст у Flash и при 0 "
        "немного гуляет (69/91 одинаковых, 25.09), при 0.3 — заметно",
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
        "--llm-provider",
        choices=("yandex", "mws", "cloudru"),
        default="yandex",
        help="поставщик модели ответа (план резерва 15.10): mws, cloudru — "
        "OpenAI-совместимый API, ключ и адрес из .env; --model — имя у поставщика",
    )
    parser.add_argument(
        "--embedding-dim", type=int, default=None, help="по умолчанию 768 для v2"
    )
    parser.add_argument("--limit", type=int, default=5, help="faq_limit (E4)")
    parser.add_argument(
        "--max-distance",
        type=float,
        default=DEFAULT_MAX_DISTANCE,
        help="0.59 — для v2-768 (30.09, ML-4; было 0.51); для text-search — 0.65",
    )
    parser.add_argument(
        "--gate-distance",
        type=float,
        default=None,
        help="отвечать по документам, если ближайшая выдержка не дальше этого "
        "(замер 04.10); выдержки — до --max-distance, а если ближайшая дальше "
        "него — до ближайшей + --near-margin",
    )
    parser.add_argument("--near-margin", type=float, default=0.05)
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
    add_rerank_arguments(parser)
    parser.add_argument(
        "--llm-extra",
        default="",
        help="ML-5: JSON с полями запроса модели ответа, например "
        '{"reasoning_effort": "none"}',
    )
    parser.add_argument(
        "--agent",
        action="store_true",
        help="вариант 1 (agent-v1): поиск сначала, модель с инструментом "
        "search_documents может искать ещё (не больше 2 доборов) и отвечать "
        "сама на реплики; дальше — общий ответ или отказ, как без флага",
    )
    parser.add_argument(
        "--decision",
        choices=("off", "separate", "merged"),
        default="off",
        help="попытка 2 (decision-v1): шаг решения строгим JSON до ответа; "
        "separate — решение и ответ разными вызовами, merged — одним",
    )
    parser.add_argument(
        "--decision-reasoning",
        action="store_true",
        help="поле рассуждения первым в схеме решения",
    )
    parser.add_argument(
        "--decision-provider",
        choices=("yandex", "mws", "cloudru"),
        default="",
        help="поставщик модели решения (по умолчанию — как --llm-provider)",
    )
    parser.add_argument("--decision-model", default="", help="модель решения")
    parser.add_argument(
        "--decision-max-tokens",
        type=int,
        default=2000,
        help="лимит выхода шага решения (у gpt-oss с рассуждением medium 1000 "
        "мало: обрезка — M-008, этап 1)",
    )
    parser.add_argument(
        "--decision-extra",
        default="",
        help="JSON с полями запроса модели решения, например "
        '{"reasoning_effort": "low"}',
    )
    parser.add_argument(
        "--decision-fast-extra",
        default="",
        help="§7.4 п. 2: JSON полей быстрого вызова решения, например "
        '{"reasoning_effort": "low"}; при отказе («ничего», «про другое», '
        "sufficiency none) решение переспрашивается с --decision-extra",
    )
    parser.add_argument(
        "--decision-recheck",
        choices=RECHECK_MODES,
        default="refusals",
        help="что перепроверять при --decision-fast-extra: только отказы "
        "(вариант R) или ещё «хватает» при названной lookup_role (R2)",
    )
    parser.add_argument(
        "--decision-rule",
        action="store_true",
        help="попытка 3: реплики и вопросы об ассистенте — правилом в коде, без модели",
    )
    parser.add_argument(
        "--decision-speculative",
        action="store_true",
        help="попытка 3: ответ Flash по первым выдержкам параллельно с решением",
    )
    parser.add_argument(
        "--decision-directory",
        action="store_true",
        help="decision-v4: справочник оргструктуры как таблица — поиск по должности "
        "кодом вместо векторного добора (документ корпуса с «оргструктур» / "
        "«контакт» / «справочник» в названии)",
    )
    parser.add_argument(
        "--decision-only",
        action="store_true",
        help="этап 1: только первое решение по найденным выдержкам — без "
        "добора и ответа",
    )
    parser.add_argument(
        "--small-talk",
        action="store_true",
        help="v2.10 (BH-45): в общем ответе правило «не вопрос» — приветствие, "
        "благодарность, вопрос об ассистенте — ответ без пометки «ответа нет»",
    )
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
    add_doc_context_arguments(parser)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--config", default="", help="имя прогона (иначе — из флагов)")
    parser.add_argument("--out", type=Path, default=RESULTS_DIR)
    parser.add_argument("--notes", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    from eval.providers import OpenAICompatibleClient
    from eval.yandex import YandexClient

    llm_extra = json.loads(args.llm_extra) if args.llm_extra else None

    chunking = ChunkingConfig(
        chunk_tokens=args.chunk_tokens,
        overlap_tokens=args.overlap_tokens,
        doc_context=args.doc_context,
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
    if args.decision != "off" and args.agent:
        print("--decision: не вместе с --agent")
        return 2
    if args.decision_only and args.decision == "off":
        print("--decision-only: только с --decision")
        return 2
    if args.decision_fast_extra and (args.decision == "off" or args.decision_only):
        print("--decision-fast-extra: только с --decision, без --decision-only")
        return 2
    if args.decision_recheck != "refusals" and not args.decision_fast_extra:
        print("--decision-recheck: только с --decision-fast-extra")
        return 2
    if args.rerank and (retriever != "vector" or args.multi_query):
        print("--rerank: только с --retriever vector и без --multi-query")
        return 2
    if (
        args.rerank
        and args.gate_distance is not None
        and args.rerank_max_distance is not None
    ):
        # С BH-37 порог реранкера — порог продукта у каждого вопроса.
        print("--rerank-max-distance: не вместе с --gate-distance")
        return 2
    rr_name = (
        config_suffix(
            args.rerank_depth,
            args.rerank_max_length,
            args.rerank_text,
            quantize=args.rerank_quantize,
        )
        + rerank_rule_suffix(args.rerank_order, args.rerank_max_words)
        if args.rerank
        else ""
    )
    config = args.config or (
        f"offline-{args.model}-{chunking.name}-{retriever}{embedder}"
        f"-k{args.limit}-d{args.max_distance}-{mode}{context_name}{mq_name}{rr_name}"
        + ("-dry" if args.dry_run else "")
    )

    documents = with_doc_context(args, load_corpus(args.corpus))
    chunks = chunk_corpus(documents, chunking)
    directory_search: (
        Callable[[str, str], tuple[list[ContextBlock[OfflineMatch]], bool]] | None
    ) = None
    if args.decision_directory:
        # Оргструктура — по названию (у стенда названия — транслит имён файлов)
        # или по заголовку в первой строке; глоссарий и прочие «справочники» —
        # нет.
        by_title = re.compile(r"orgstruktura|оргструктур|org[_ -]?chart", re.IGNORECASE)
        by_head = re.compile(r"организационн\w+\s+структур", re.IGNORECASE)
        source = next(
            (
                d
                for d in documents
                if by_title.search(d.title) or by_head.search(d.markdown[:200])
            ),
            None,
        )
        # Нет справочника (корпус dev) — как в продукте с пустой таблицей:
        # поле lookup_role есть, но маршрут — обычный векторный добор.
        if source is None:
            print("--decision-directory: в корпусе нет справочника — добор вектором")
        else:
            directory_search = _directory_search(source.title, source.markdown)
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

    reranker = (
        make_reranker(args.rerank, args.rerank_max_length) if args.rerank else None
    )
    if reranker is not None:
        reranker.trust_remote_code = args.rerank_trust_remote_code
        reranker.quantize = args.rerank_quantize
    search_options = dict(
        retriever=retriever,
        limit=args.limit,
        weights=[float(w) for w in args.weights.split(",")],
        rrf_k=args.rrf_k,
        workers=args.workers,
        embedding_model=args.embedding_model,
        embedding_dim=embedding_dim,
        paraphrases=[p.queries for p in paraphrased] if args.multi_query else None,
        paraphrase_weight=args.mq_weight,
        reranker=reranker,
        rerank_depth=args.rerank_depth,
        # Реранкер выбирает лучших среди прошедших порог продукта.
        rerank_max_distance=(
            args.rerank_max_distance
            if args.rerank_max_distance is not None
            else args.max_distance
        ),
        rerank_kind=args.rerank_text,
        gate_distance=args.gate_distance,
        near_margin=args.near_margin,
        rerank_order=args.rerank_order,
        rerank_max_words=args.rerank_max_words,
    )
    retrieved = retrieve(chunks, [item.question for item in items], **search_options)

    def search_more(query: str) -> tuple[list[OfflineMatch], float | None]:
        """faq-v2.9: второй шаг поиска — тот же поиск, что у вопроса."""
        return retrieve(chunks, [query], **{**search_options, "paraphrases": None})[0]

    if reranker is not None:
        reranker.save()
        print(
            f"Реранкер {args.rerank}: пар посчитано {reranker.scored_pairs}, "
            f"{reranker.scoring_ms / 1000:.1f} с (остальное из кэша)"
        )
    # --dry-run: LLM не вызывается — поиск и контекст замеряются бесплатно.
    client: YandexClient | OpenAICompatibleClient | None = None
    if not args.dry_run:
        client = (
            YandexClient.from_env()
            if args.llm_provider == "yandex"
            else OpenAICompatibleClient.from_env(args.llm_provider)
        )

    def ask(messages: list[Message]) -> Completion:
        assert client is not None
        return client.complete(
            messages,
            model=args.model,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            api=args.api,
            **({"extra": llm_extra} if llm_extra else {}),
        )

    def agent_search(query: str) -> list[ContextBlock[OfflineMatch]]:
        """Добор агента: тот же поиск и порог BH-37, что у вопроса."""
        more, more_best = search_more(query)
        gated = relevant_matches(
            more,
            args.max_distance,
            gate_distance=args.gate_distance,
            near_margin=args.near_margin,
            nearest=more_best,
        )
        return build_context(
            gated,
            mode=context_mode,
            sections=sections,
            max_tokens=args.context_tokens,
            neighbours=args.neighbours,
        )

    def agent_chat(
        messages: list[dict[str, Any]], tools: Sequence[dict[str, Any]]
    ) -> Completion:
        assert client is not None
        return client.chat_with_tools(
            messages,
            model=args.model,
            tools=tools,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            extra=llm_extra,
        )

    decision_client: YandexClient | OpenAICompatibleClient | None = None
    decision_extra = json.loads(args.decision_extra) if args.decision_extra else None
    decision_provider = args.decision_provider or args.llm_provider
    if args.decision != "off" and not args.dry_run:
        decision_client = (
            client
            if decision_provider == args.llm_provider
            else YandexClient.from_env()
            if decision_provider == "yandex"
            else OpenAICompatibleClient.from_env(decision_provider)
        )
    decision_format = decision_response_format(
        reasoning=args.decision_reasoning, answer=args.decision == "merged"
    )

    def decide(
        messages: list[Message], extra: dict[str, object] | None = None
    ) -> Completion:
        assert decision_client is not None
        extra = extra or decision_extra
        return decision_client.complete(
            messages,
            model=args.decision_model or args.model,
            temperature=args.temperature,
            max_tokens=args.decision_max_tokens,
            api="openai",
            response_format=decision_format,
            **(
                {"extra": extra}
                if extra
                else {"extra": llm_extra}
                if llm_extra and decision_provider == args.llm_provider
                else {}
            ),
        )

    # §7.4 п. 2: быстрый вызов решения (low), отказ — перепроверка decide.
    fast_extra = (
        json.loads(args.decision_fast_extra) if args.decision_fast_extra else None
    )

    def decide_fast(messages: list[Message]) -> Completion:
        return decide(messages, fast_extra)

    rows: list[dict[str, object]] = []
    for number, (item, (found, best)) in enumerate(
        zip(items, retrieved, strict=True), start=1
    ):
        # С multi-query у чанка из переформулировки нет расстояния до вопроса,
        # поэтому порог — по лучшему расстоянию исходного вопроса (как M1).
        relevant = (
            relevant_matches(
                found,
                args.max_distance,
                gate_distance=args.gate_distance,
                near_margin=args.near_margin,
                nearest=best,
            )
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
        small = False
        hop_query, hop_added = "", 0
        agent_run: AgentRun | None = None
        decided: DecidedRun | None = None
        decision_calls: list[Completion] = []
        first_decision = ""
        if args.decision_only and not args.dry_run:
            calls.append(
                decide(
                    build_decision_messages(
                        item.question,
                        selected,
                        reasoning=args.decision_reasoning,
                        answer=args.decision == "merged",
                    )
                )
            )
            decision_calls.append(calls[-1])
            parsed = parse_decision(calls[-1].text)
            first_decision = (
                parsed.model_dump_json()
                if parsed
                else "INVALID " + calls[-1].text[:300]
            )
            raw = answer = ""
        elif args.decision != "off" and not args.dry_run:
            decided = run_decided(
                item.question,
                selected,
                decide=decide,
                answer=ask,
                search=agent_search,
                reasoning=args.decision_reasoning,
                merged=args.decision == "merged",
                rule=args.decision_rule,
                speculative=args.decision_speculative,
                directory=directory_search,
                decide_fast=decide_fast if fast_extra else None,
                recheck=args.decision_recheck,
            )
            calls.extend(decided.calls)
            decision_calls.extend(decided.decision_calls)
            raw = decided.text
            selected = decided.shown
            context_text = "\n".join(block.content for block in selected)
            small = decided.small_talk
            answer = (
                raw
                if small or is_not_found(raw)
                else normalize_citations(raw, selected)
            )
        elif args.agent and not args.dry_run:
            agent_run = run_agent(
                item.question, selected, chat=agent_chat, search=agent_search
            )
            calls.extend(agent_run.calls)
            raw = agent_run.text or NOT_FOUND_ANSWER
            selected = agent_run.shown
            context_text = "\n".join(block.content for block in selected)
            reply = small_talk_reply(raw)
            if reply is not None:
                small, answer = True, drop_citations(reply)
            elif not is_not_found(raw):
                answer = normalize_citations(raw, selected)
        elif selected and not args.dry_run:
            calls.append(
                ask(build_faq_messages(item.question, selected, search="allowed"))
            )
            raw = calls[-1].text.strip()
            # faq-v2.9: модель просит второй шаг поиска — тот же поиск по её
            # запросу, новые фрагменты (до SEARCH_MORE_LIMIT) — после прежних.
            query = search_request(raw)
            if query is not None and retriever == "vector" and not args.multi_query:
                hop_query = query
                more, more_best = search_more(query)
                known = {match.content for match in relevant}
                added = [
                    match
                    for match in relevant_matches(
                        more,
                        args.max_distance,
                        gate_distance=args.gate_distance,
                        near_margin=args.near_margin,
                        nearest=more_best,
                    )
                    if match.content not in known
                ][:SEARCH_MORE_LIMIT]
                hop_added = len(added)
                raw = NOT_FOUND_ANSWER
                if added:
                    selected = build_context(
                        [*relevant, *added],
                        mode=context_mode,
                        sections=sections,
                        max_tokens=args.context_tokens,
                        neighbours=args.neighbours,
                    )
                    context_text = "\n".join(block.content for block in selected)
                    calls.append(
                        ask(build_faq_messages(item.question, selected, search="done"))
                    )
                    raw = calls[-1].text.strip()
                    if search_request(raw) is not None:
                        # Второй раз не ищем: ответа по документам нет.
                        raw = NOT_FOUND_ANSWER
            elif query is not None:
                raw = NOT_FOUND_ANSWER
            answer = normalize_citations(raw, selected)
        # Р1 (решено 25.09): ответа в документах нет — ни одна выдержка не
        # прошла порог или модель по выдержкам ответила отказом — общий ответ
        # со строгой пометкой, что он не из документов компании.
        # Агент: общий ответ — только если модель отказала (реплика — уже
        # ответ); без агента — как раньше.
        needs_general = (
            False
            if args.decision_only
            else decided.needs_general
            if decided is not None
            else not small and is_not_found(raw)
            if agent_run is not None
            else not selected or is_not_found(raw)
        )
        if decided is not None and decided.small_talk:
            pass  # реплика — уже ответ (decision-v1)
        elif (
            mode == "general"
            and not args.dry_run
            and needs_general
            and decided is not None
        ):
            # Решение: ответа по документам нет — общий ответ с пометкой; на
            # реплики решение отвечает само, поэтому промпт общего ответа
            # прежний (без правила реплик).
            calls.append(ask(build_general_messages(item.question)))
            answer = with_other_subject_note(
                ensure_general_prefix(calls[-1].text.strip()), decided.decision
            )
            general = True
        elif mode == "general" and not args.dry_run and needs_general:
            calls.append(
                ask(build_general_messages(item.question, small_talk=args.small_talk))
            )
            # v2.10 (BH-45): «не вопрос» — ответ без пометки «ответа нет».
            if args.small_talk:
                small, answer = split_general_answer(calls[-1].text.strip())
                general = not small
            else:
                answer = ensure_general_prefix(calls[-1].text.strip())
                general = True

        if decided is not None and mode == "strict" and needs_general:
            answer = with_other_subject_note(NOT_FOUND_ANSWER, decided.decision)
        # Решение (попытка 3): время по стене — параллельная пара как максимум.
        latency = (
            decided.wall_ms if decided is not None else sum(c.latency_ms for c in calls)
        )
        if decided is not None and general:
            latency += calls[-1].latency_ms  # общий ответ — после цикла
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
                "search_query": hop_query,
                "search_added": hop_added,
                "answered": answered,
                "answer_given": bool(selected),
                "general_answer": general,
                "small_talk": small,
                "agent_queries": json.dumps(agent_run.queries, ensure_ascii=False)
                if agent_run
                else "",
                "agent_added": json.dumps(agent_run.added) if agent_run else "",
                "agent_limited": agent_run.refused_by_limit if agent_run else "",
                "agent_markup": agent_run.raw_markup if agent_run else "",
                "agent_forced": agent_run.forced_final if agent_run else "",
                "decision_route": decided.route.value if decided else "",
                "decision": first_decision
                or (
                    json.dumps(decided.decisions, ensure_ascii=False) if decided else ""
                ),
                "decision_queries": json.dumps(decided.queries, ensure_ascii=False)
                if decided
                else "",
                "decision_added": json.dumps(decided.added) if decided else "",
                "decision_invalid": decided.invalid if decided else "",
                "decision_calls": len(decision_calls),
                "decision_input_tokens": sum(c.input_tokens for c in decision_calls),
                "decision_output_tokens": sum(c.output_tokens for c in decision_calls),
                "decision_latency_ms": round(sum(c.latency_ms for c in decision_calls)),
                "decision_exhausted": decided.exhausted if decided else "",
                "decision_by_rule": decided.by_rule if decided else "",
                "decision_speculated": decided.speculated if decided else "",
                "decision_wasted": decided.wasted if decided else "",
                "decision_directory": (
                    f"{decided.directory_hits}/{decided.directory_misses}"
                    f"/{decided.directory_partial}"
                    if decided
                    else ""
                ),
                "decision_rechecked": decided.rechecked > 0 if decided else "",
                "decision_recheck_reason": ",".join(decided.recheck_reasons)
                if decided
                else "",
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
        "small_talk": sum(1 for row in rows if row["small_talk"]),
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
    prompt_version = (
        DECISION_PROMPT_VERSION
        + f"-{args.decision}"
        + ("-r" if args.decision_reasoning else "")
        + ("-rule" if args.decision_rule else "")
        + ("-spec" if args.decision_speculative else "")
        + ("-dir" if args.decision_directory else "")
        + ("-recheck" if args.decision_fast_extra else "")
        + ("-role" if args.decision_recheck == "refusals+role" else "")
        + (f"-{decision_provider}" if decision_provider != args.llm_provider else "")
        + ("-only" if args.decision_only else "")
        if args.decision != "off"
        else SMALL_TALK_PROMPT_VERSION
        if args.small_talk
        else SEARCH_PROMPT_VERSION
    )
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
            or f"{prompt_version}; "
            f"{args.llm_provider}:{args.model}; "
            f"{chunking.name}; ctx={args.context_tokens}; "
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
