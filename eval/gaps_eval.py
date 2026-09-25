"""Оценка отчёта о пробелах на синтетике (задача ML 3.4).

    python -m eval.gaps_eval --corpus corpus/ --remove UMNIK --remove Pravila \\
        --dataset silver.csv --dataset golden.csv

По умолчанию — решение по задаче 1: text-embeddings-v2 768, порог 0.51.

Идея: из корпуса убираем 1–2 документа. Вопросы по убранным — истинные
пробелы (в базе этого нет), по оставшимся — не пробелы. Прогоняем поиск
по урезанному корпусу, считаем сигналы и classify_miss — те же функции,
что вызовет бэкенд (corp_ed.domain.gaps).

Без вызова LLM: gap возможен, только когда вектор НЕ прошёл порог — тогда
модель не вызывается и в продукте. Если вектор прошёл порог, вопрос не gap
(answered / model_refusal); что именно — видно только по ответу модели:
--answers <результаты offline_e2e на том же урезанном корпусе> (колонка
answered). Без них такие вопросы — «context».

Считаем два варианта отчёта: «только gap» и «gap + отказ модели при
найденных выдержках» — близкие по теме пробелы порог не отсекает (задача
3.4, 25.09), и их видно только по отказам.

Полнотекст: в продукте ts_rank_cd из Postgres; здесь его замена — доля
слов вопроса (основ, без стоп-слов), найденных в лучшем по BM25 чанке.
Пороги strong / empty для ts_rank_cd подбираются заново на живых логах.

Кластеры: вопросы, классифицированные как gap, кластеризуются
cluster_questions по векторам вопросов; чистота — доля вопросов кластера
из документа большинства.
"""

import argparse
import csv
import sys
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from corp_ed.domain.gaps import (
    GapThresholds,
    MissKind,
    MissSignals,
    classify_miss,
    cluster_questions,
)
from eval.bm25 import BM25Index, tokenize
from eval.corpus import ChunkingConfig, chunk_corpus, load_corpus
from eval.datasets import EvalItem, load_dataset
from eval.relevance import normalize_material
from eval.yandex import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_MAX_DISTANCE,
    default_embedding_dim,
)


def fulltext_coverage(question: str, index: BM25Index, texts: Sequence[str]) -> float:
    """Доля основ слов вопроса в лучшем по BM25 чанке (0 — ничего)."""
    terms = set(tokenize(question))
    if not terms:
        return 0.0
    hits = index.search(question, 1)
    if not hits or hits[0][1] <= 0:
        return 0.0
    best = set(tokenize(texts[hits[0][0]]))
    return len(terms & best) / len(terms)


def purity(labels: Sequence[int], truth: Sequence[str]) -> float:
    """Доля вопросов, попавших в кластер вместе с большинством своего класса."""
    by_cluster: dict[int, list[str]] = defaultdict(list)
    for label, value in zip(labels, truth, strict=True):
        by_cluster[label].append(value)
    agree = sum(Counter(values).most_common(1)[0][1] for values in by_cluster.values())
    return agree / len(truth) if truth else 0.0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m eval.gaps_eval", description=__doc__
    )
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument(
        "--remove",
        action="append",
        required=True,
        help="подстрока названия убираемого документа",
    )
    parser.add_argument("--dataset", type=Path, action="append", required=True)
    parser.add_argument("--max-distance", type=float, default=DEFAULT_MAX_DISTANCE)
    parser.add_argument("--embedding-model", default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument(
        "--embedding-dim", type=int, default=None, help="по умолчанию 768 для v2"
    )
    parser.add_argument("--empty", type=float, nargs="+", default=[0.3, 0.4, 0.5])
    parser.add_argument(
        "--strong", type=float, nargs="+", default=[0.6, 0.7, 0.8, 1.01]
    )
    parser.add_argument(
        "--cluster-distance", type=float, nargs="+", default=[0.3, 0.4, 0.5]
    )
    parser.add_argument(
        "--answers",
        type=Path,
        action="append",
        default=[],
        help="результаты offline_e2e на урезанном корпусе (id → answered)",
    )
    parser.add_argument("--out", type=Path, default=Path("eval/private/results/gaps"))
    args = parser.parse_args(argv)
    embedding_dim = args.embedding_dim or default_embedding_dim(args.embedding_model)

    from eval.bench import vector_rankings
    from eval.yandex import EmbeddingCache, YandexClient, embed_many

    documents = load_corpus(args.corpus)
    removed = {
        d.title for d in documents if any(part in d.title for part in args.remove)
    }
    kept_docs = [d for d in documents if d.title not in removed]
    chunks = chunk_corpus(
        kept_docs, ChunkingConfig(chunk_tokens=400, overlap_tokens=50)
    )
    removed_norm = {normalize_material(t) for t in removed}
    kept_norm = {normalize_material(d.title) for d in kept_docs}
    print(f"Убраны: {sorted(removed)}; осталось чанков: {len(chunks)}")

    items: list[EvalItem] = []
    for path in args.dataset:
        items += load_dataset(path)

    def truth(item: EvalItem) -> str:
        if not item.in_corpus:
            return "out"
        material = normalize_material(item.expected_material)
        if material in removed_norm:
            return "gap"
        if material in kept_norm:
            return "not_gap"
        return "unknown"

    answered: dict[str, bool] = {}
    for path in args.answers:
        with path.open(encoding="utf-8") as file:
            for row in csv.DictReader(file):
                answered[row["id"]] = row["answered"] == "True"

    labelled = [(item, truth(item)) for item in items]
    labelled = [(item, t) for item, t in labelled if t != "unknown"]
    questions = [item.question for item, _ in labelled]

    _, distances = vector_rankings(
        chunks, questions, 1, 4, args.embedding_model, embedding_dim
    )
    texts = [c.embed_text for c in chunks]
    index = BM25Index(texts)
    coverage = [fulltext_coverage(q, index, texts) for q in questions]
    best = [d[0] if d else None for d in distances]

    client = YandexClient.from_env(
        embedding_model=args.embedding_model, embedding_dim=embedding_dim
    )
    cache = EmbeddingCache(Path("eval/.cache/embeddings.sqlite"))
    query_vectors = [e.vector for e in embed_many(client, questions, "query", cache)]
    cache.close()

    lines = [
        f"# Отчёт о пробелах — синтетика, {date.today().isoformat()}",
        "",
        f"Корпус без: {', '.join(sorted(removed))}. Вопросов: "
        + ", ".join(f"{k} {v}" for k, v in Counter(t for _, t in labelled).items())
        + f". Эмбеддер {args.embedding_model}"
        + (f" {embedding_dim}" if embedding_dim else "")
        + f", порог {args.max_distance}.",
        "",
        "| empty | strong | gap: precision | recall | F1 "
        "| вне корпуса → gap | → retrieval_miss | → unclear |",
        "|---|---|---|---|---|---|---|---|",
    ]
    best_f1, best_cfg, best_kinds = -1.0, (0.0, 0.0), []
    for empty in args.empty:
        for strong in args.strong:
            if empty > strong:
                continue
            thresholds = GapThresholds(
                max_distance=args.max_distance,
                strong_fulltext=strong,
                empty_fulltext=empty,
            )
            kinds = []
            for (item, _), distance, cov in zip(labelled, best, coverage, strict=True):
                if distance is not None and distance <= args.max_distance:
                    if item.id in answered:
                        signals = MissSignals(distance, cov, answered[item.id])
                        kinds.append(classify_miss(signals, thresholds).value)
                    else:
                        kinds.append("context")
                else:
                    kinds.append(
                        classify_miss(
                            MissSignals(distance, cov, False), thresholds
                        ).value
                    )
            pairs = list(zip(kinds, (t for _, t in labelled), strict=True))
            tp = sum(1 for k, t in pairs if k == MissKind.GAP and t == "gap")
            fp = sum(1 for k, t in pairs if k == MissKind.GAP and t == "not_gap")
            fn = sum(1 for k, t in pairs if k != MissKind.GAP and t == "gap")
            p = tp / (tp + fp) if tp + fp else 0.0
            r = tp / (tp + fn) if tp + fn else 0.0
            f1 = 2 * p * r / (p + r) if p + r else 0.0
            out = [k for k, t in pairs if t == "out"]
            share = {k: out.count(k) for k in ("gap", "retrieval_miss", "unclear")}
            n_out = len(out)
            lines.append(
                f"| {empty} | {strong} | {p:.2f} ({tp}/{tp + fp}) "
                f"| {r:.2f} ({tp}/{tp + fn}) | {f1:.2f} | {share['gap']}/{n_out} "
                f"| {share['retrieval_miss']}/{n_out} | {share['unclear']}/{n_out} |"
            )
            if f1 > best_f1:
                best_f1, best_cfg, best_kinds = f1, (empty, strong), kinds

    truth_all = [t for _, t in labelled]
    if answered:
        lines += [
            "",
            "С отказами модели («gap + model_refusal») при лучшем варианте:",
            "",
        ]
        report_kinds = {MissKind.GAP.value, MissKind.MODEL_REFUSAL.value}
        pairs = list(zip(best_kinds, truth_all, strict=True))
        tp = sum(1 for k, t in pairs if k in report_kinds and t == "gap")
        fp = sum(1 for k, t in pairs if k in report_kinds and t == "not_gap")
        fn = sum(1 for k, t in pairs if k not in report_kinds and t == "gap")
        p = tp / (tp + fp) if tp + fp else 0.0
        r = tp / (tp + fn) if tp + fn else 0.0
        false_answers = sum(1 for k, t in pairs if k == "answered" and t == "gap")
        lines += [
            f"- precision {p:.2f} ({tp}/{tp + fp}), recall {r:.2f} ({tp}/{tp + fn});",
            f"- модель ответила на вопрос по убранному документу (ложный ответ): "
            f"{false_answers} из {sum(1 for t in truth_all if t == 'gap')}.",
        ]
    lines += [
        "",
        f"Лучший вариант: empty {best_cfg[0]}, strong {best_cfg[1]} (подобран на этих "
        "же данных — оценка оптимистичная).",
        "",
        "Где вопросы по убранным документам (истинные пробелы) при лучшем варианте:",
        "",
    ]
    lines += [
        f"- {kind}: {count}"
        for kind, count in Counter(
            k for k, t in zip(best_kinds, truth_all, strict=True) if t == "gap"
        ).most_common()
    ]

    gap_idx = [i for i, k in enumerate(best_kinds) if k == MissKind.GAP]
    doc_of = [
        normalize_material(item.expected_material) if t != "out" else f"out:{item.id}"
        for item, t in labelled
    ]
    lines += [
        "",
        "## Кластеры gap-вопросов",
        "",
        "| max_distance | кластеров | вопросов | чистота по документу | крупнейшие |",
        "|---|---|---|---|---|",
    ]
    rows_out = []
    for threshold in args.cluster_distance:
        vectors = [query_vectors[i] for i in gap_idx]
        labels = cluster_questions(vectors, max_distance=threshold)
        docs = [doc_of[i] for i in gap_idx]
        sizes = Counter(labels).most_common(3)
        lines.append(
            f"| {threshold} | {len(set(labels))} | {len(gap_idx)} | "
            f"{purity(labels, docs):.2f} | {', '.join(str(n) for _, n in sizes)} |"
        )
        for i, label in zip(gap_idx, labels, strict=True):
            rows_out.append(
                {
                    "cluster_distance": threshold,
                    "cluster": label,
                    "id": labelled[i][0].id,
                    "truth": labelled[i][1],
                    "doc": doc_of[i],
                    "question": questions[i],
                }
            )

    args.out.mkdir(parents=True, exist_ok=True)
    stem = f"{date.today().isoformat()}_gaps_eval"
    with (args.out / f"{stem}_questions.csv").open(
        "w", encoding="utf-8", newline=""
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "id",
                "truth",
                "best_distance",
                "fulltext_coverage",
                "kind",
                "question",
            ],
        )
        writer.writeheader()
        for (item, t), d, cov, k in zip(
            labelled, best, coverage, best_kinds, strict=True
        ):
            writer.writerow(
                {
                    "id": item.id,
                    "truth": t,
                    "best_distance": d,
                    "fulltext_coverage": round(cov, 3),
                    "kind": k,
                    "question": item.question,
                }
            )
    with (args.out / f"{stem}_clusters.csv").open(
        "w", encoding="utf-8", newline=""
    ) as f:
        writer = csv.DictWriter(f, fieldnames=list(rows_out[0]) if rows_out else ["id"])
        writer.writeheader()
        writer.writerows(rows_out)
    report = "\n".join(lines)
    print(report)
    (args.out / f"{stem}.md").write_text(report + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
