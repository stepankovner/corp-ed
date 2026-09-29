"""Офлайн-стенд поиска: эксперименты E1–E3 и предпросмотр гибрида M1 без бэкенда.

Официальные числа — через run_eval (HTTP API бэкенда). Стенд нужен, чтобы
перебирать нарезки, не дожидаясь переингеста на бэкенде: он сам режет
корпус, сам считает эмбеддинги (или BM25) и ищет в памяти.

    # E1: нарезка v1 против v2 (BM25 — без ключей; vector — нужны YC_* ключи)
    python -m eval.bench --corpus corpus/ --dataset eval/silver.csv --chunker v1
    python -m eval.bench --corpus corpus/ --dataset eval/silver.csv --chunker v2

    # E2: без крошек в embed_text / крошки без названия документа
    python -m eval.bench ... --chunker v2 --no-crumbs --retriever vector
    python -m eval.bench ... --chunker v2 --crumbs-without-title --retriever vector

    # E3: сетка размеров
    python -m eval.bench ... --chunk-tokens 250 --overlap-tokens 0

    # M1 (предпросмотр): гибрид вектор + BM25 через RRF
    python -m eval.bench ... --retriever hybrid --weights 1.0,0.5

    # M5 (предпросмотр): вопросы, расширенные словарём сокращений
    python -m eval.bench ... --glossary glossary.csv
    # M6 (эксперимент): переформулировки вопроса моделью + RRF (вызов LLM с кэшем)
    python -m eval.bench ... --multi-query 3 [--mq-weight 0.5]

Сравнить два прогона статистически: python -m eval.compare A.csv B.csv
"""

import argparse
import csv
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from corp_ed.domain.fusion import rrf_merge
from corp_ed.domain.query import expand_query, fuse_query_rankings
from corp_ed.prompts.multi_query import PROMPT_VERSION as MQ_PROMPT_VERSION
from eval.corpus import BenchChunk, ChunkingConfig, chunk_corpus, load_corpus
from eval.datasets import EvalItem, load_dataset, select_split
from eval.multi_query import paraphrase_questions
from eval.relevance import RetrievedChunk
from eval.results import RESULTS_DIR, append_summary, results_path, write_csv
from eval.retrieval_eval import evaluate_retrieval, format_report
from eval.yandex import (
    DEFAULT_API,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_LLM,
    default_embedding_dim,
)

CACHE_PATH = Path("eval/.cache/embeddings.sqlite")
FUSION_CANDIDATES = 50


def _retrieved(chunk: BenchChunk, distance: float | None) -> RetrievedChunk:
    return RetrievedChunk(
        id=chunk.id,
        material=chunk.material,
        content=chunk.llm_text,
        heading_path=chunk.heading_path,
        distance=distance,
    )


def bm25_rankings(
    chunks: Sequence[BenchChunk], queries: Sequence[str], limit: int
) -> list[list[int]]:
    from eval.bm25 import BM25Index

    index = BM25Index([chunk.embed_text for chunk in chunks])
    return [[i for i, _ in index.search(query, limit)] for query in queries]


def vector_rankings(
    chunks: Sequence[BenchChunk],
    queries: Sequence[str],
    limit: int,
    workers: int,
    embedding_model: str = "text-search",
    embedding_dim: int | None = None,
) -> tuple[list[list[int]], list[list[float]]]:
    """Индексы чанков по близости и косинусные расстояния (1 − косинус)."""
    import numpy as np

    from eval.yandex import EmbeddingCache, YandexClient, embed_many

    client = YandexClient.from_env(
        embedding_model=embedding_model, embedding_dim=embedding_dim
    )
    cache = EmbeddingCache(CACHE_PATH)

    def progress(done: int, total: int) -> None:
        if done % 50 == 0 or done == total:
            print(f"  эмбеддинги: {done}/{total}")

    print(f"Эмбеддинги чанков ({client.model_uri('doc')}), {len(chunks)} шт.")
    docs = embed_many(
        client,
        [c.embed_text for c in chunks],
        "doc",
        cache,
        workers=workers,
        progress=progress,
    )
    print(f"Эмбеддинги вопросов, {len(queries)} шт.")
    questions = embed_many(client, list(queries), "query", cache, workers=workers)
    cache.close()

    doc_matrix = np.array([e.vector for e in docs], dtype=np.float32)
    query_matrix = np.array([e.vector for e in questions], dtype=np.float32)
    # text-search отдаёт нормализованные векторы (разведка 13.09), но другие
    # модели каталога — не обязательно: нормируем сами, косинус = скалярное.
    doc_matrix /= np.linalg.norm(doc_matrix, axis=1, keepdims=True)
    query_matrix /= np.linalg.norm(query_matrix, axis=1, keepdims=True)
    similarity = query_matrix @ doc_matrix.T
    order = np.argsort(-similarity, axis=1)[:, :limit]
    rankings = [row.tolist() for row in order]
    distances = [
        [float(1 - similarity[q, i]) for i in row] for q, row in enumerate(order)
    ]
    return rankings, distances


def load_glossary(path: Path) -> dict[str, str]:
    """CSV с колонками term,expansion — как будущая таблица glossary."""
    with path.open(encoding="utf-8-sig", newline="") as file:
        return {row["term"]: row["expansion"] for row in csv.DictReader(file)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.bench",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--corpus", type=Path, required=True, help="папка с документами"
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--split", default="", help="только вопросы split (dev/test)")
    parser.add_argument("--chunker", choices=("v1", "v2"), default="v2")
    parser.add_argument("--chunk-tokens", type=int, default=400)
    parser.add_argument("--overlap-tokens", type=int, default=50)
    parser.add_argument("--chunk-size", type=int, default=1000, help="v1: символы")
    parser.add_argument("--overlap", type=int, default=100, help="v1: символы")
    parser.add_argument(
        "--no-crumbs", action="store_true", help="E2: крошки не в эмбеддинг"
    )
    parser.add_argument(
        "--crumbs-without-title",
        action="store_true",
        help="E2: в крошках эмбеддинга только заголовки разделов",
    )
    parser.add_argument(
        "--retriever", choices=("vector", "bm25", "hybrid"), default="bm25"
    )
    parser.add_argument(
        "--weights", default="1.0,0.5", help="hybrid: вектор,полнотекст"
    )
    parser.add_argument("--rrf-k", type=int, default=60)
    parser.add_argument(
        "--embedding-model",
        default=DEFAULT_EMBEDDING_MODEL,
        help="семейство эмбеддингов: text-embeddings-v2 (решение 25.09), text-search",
    )
    parser.add_argument(
        "--embedding-dim",
        type=int,
        default=None,
        help="размерность v2: 128, 256, 512, 768 (по умолчанию — 768)",
    )
    parser.add_argument("--k", type=int, default=10, help="top-K выдачи")
    parser.add_argument("--glossary", type=Path, help="CSV term,expansion (M5)")
    parser.add_argument(
        "--multi-query",
        type=int,
        default=0,
        help="M6: переформулировок на вопрос (0 — выкл.; вызов LLM с кэшем)",
    )
    parser.add_argument(
        "--mq-weight", type=float, default=1.0, help="M6: вес переформулировки в RRF"
    )
    parser.add_argument("--mq-model", default=DEFAULT_LLM, help="M6: модель")
    parser.add_argument("--api", choices=("native", "openai"), default=DEFAULT_API)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--config", default="", help="имя конфигурации (по умолчанию из флагов)"
    )
    parser.add_argument("--out", type=Path, default=RESULTS_DIR)
    parser.add_argument("--notes", default="")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    chunking = ChunkingConfig(
        version=args.chunker,
        chunk_tokens=args.chunk_tokens,
        overlap_tokens=args.overlap_tokens,
        chunk_size=args.chunk_size,
        overlap=args.overlap,
        crumbs_in_embed=not args.no_crumbs,
        title_in_crumbs=not args.crumbs_without_title,
    )
    embedding_dim = args.embedding_dim or default_embedding_dim(args.embedding_model)
    dim_suffix = f"-d{embedding_dim}" if embedding_dim else ""
    embedder = (
        ""
        if args.retriever == "bm25" or args.embedding_model == "text-search"
        else f"-{args.embedding_model}{dim_suffix}"
    )
    mq_name = (
        f"-mq{args.multi_query}"
        + (f"-w{args.mq_weight}" if args.mq_weight != 1.0 else "")
        if args.multi_query
        else ""
    )
    config = (
        args.config
        or f"{chunking.name}-{args.retriever}"
        + ("-glossary" if args.glossary else "")
        + mq_name
        + embedder
    )

    documents = load_corpus(args.corpus)
    chunks = chunk_corpus(documents, chunking)
    items: list[EvalItem] = load_dataset(args.dataset)
    # Без --split holdout золотого набора не берём (select_split, задача 2.1).
    items = select_split(items, args.split)
    print(
        f"Документов: {len(documents)}, чанков: {len(chunks)}, вопросов: {len(items)}"
    )

    glossary = load_glossary(args.glossary) if args.glossary else {}
    queries = [expand_query(item.question, glossary) for item in items]

    # M6: переформулировки — один вызов LLM на вопрос, с кэшем. Ищем по
    # вопросу и по каждой переформулировке, сливаем fuse_query_rankings.
    paraphrases: list[list[str]] = [[] for _ in items]
    if args.multi_query:
        from eval.yandex import YandexClient

        paraphrases = [
            p.queries
            for p in paraphrase_questions(
                YandexClient.from_env(),
                [item.question for item in items],
                count=args.multi_query,
                model=args.mq_model,
                api=args.api,
            )
        ]
        print(
            f"Переформулировок ({MQ_PROMPT_VERSION}): "
            f"{sum(map(len, paraphrases))} на {len(items)} вопросов"
        )
    flat = [*queries, *(query for group in paraphrases for query in group)]
    depth = (
        args.k
        if args.retriever != "hybrid" and not args.multi_query
        else max(args.k, FUSION_CANDIDATES)
    )

    # Ранжирование каждого запроса: индексы чанков и расстояния векторной
    # ветки (у BM25 расстояний нет, у гибрида — только у найденных вектором).
    ranked: list[tuple[list[int], dict[int, float]]]
    if args.retriever == "bm25":
        ranked = [(ranking, {}) for ranking in bm25_rankings(chunks, flat, depth)]
    else:
        vec_rank, vec_dist = vector_rankings(
            chunks,
            flat,
            depth,
            args.workers,
            args.embedding_model,
            embedding_dim,
        )
        if args.retriever == "vector":
            ranked = [
                (ranking, dict(zip(ranking, dists, strict=True)))
                for ranking, dists in zip(vec_rank, vec_dist, strict=True)
            ]
        else:
            weights = [float(w) for w in args.weights.split(",")]
            text_rank = bm25_rankings(chunks, flat, depth)
            ranked = [
                (
                    [i for i, _ in rrf_merge([v_rank, t_rank], weights, k=args.rrf_k)],
                    dict(zip(v_rank, v_dist, strict=True)),
                )
                for v_rank, v_dist, t_rank in zip(
                    vec_rank, vec_dist, text_rank, strict=True
                )
            ]

    retrieved: dict[str, list[RetrievedChunk]] = {}
    best_vector: dict[str, float | None] = {}
    offset = len(items)
    for index, item in enumerate(items):
        ranking, distance_of = ranked[index]
        extra = [ranked[offset + j][0] for j in range(len(paraphrases[index]))]
        offset += len(extra)
        if extra:
            ranking = fuse_query_rankings(
                ranking, extra, paraphrase_weight=args.mq_weight, k=args.rrf_k
            )
        retrieved[item.id] = [
            _retrieved(chunks[i], distance_of.get(i)) for i in ranking[: args.k]
        ]
        if args.retriever != "bm25":
            # M1/M6: порог отказа — по лучшему векторному кандидату ИСХОДНОГО
            # вопроса, не по скору RRF.
            best_vector[item.id] = min(distance_of.values(), default=None)

    report = evaluate_retrieval(items, retrieved)
    for row in report.rows:
        if row["id"] in best_vector:
            row["best_vector_distance"] = best_vector[str(row["id"])]

    path = results_path(args.out, config, "bench", date.today())
    write_csv(path, report.rows)
    append_summary(
        args.out,
        {
            **report.summary,
            "date": date.today().isoformat(),
            "config": config,
            "mode": f"bench-{args.retriever}",
            "dataset": args.dataset.name + (f":{args.split}" if args.split else ""),
            "results_file": path.name,
            "notes": args.notes or f"chunks={len(chunks)}",
        },
    )
    print(format_report(report))
    print(f"Результаты: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
