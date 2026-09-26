"""Метрики поиска по выдаче: общее для run_eval (через API) и офлайн-стенда.

На вход — вопросы набора и то, что вернул поиск на каждый. На выход —
строки подробного отчёта (по вопросу) и сводка для summary.csv.

Hit Rate и MRR считаются только по вопросам из корпуса: у вопроса вне
корпуса правильного чанка нет. Зато для всех вопросов сохраняется
расстояние до ближайшего чанка — по нему выбирается порог отказа (A8).
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from eval.datasets import EvalItem
from eval.metrics import (
    bootstrap_ci,
    gold_ranks,
    hit_rate_at_k,
    mrr,
    percentile,
    rank_coverage,
    reciprocal_ranks,
)
from eval.relevance import RetrievedChunk, canonical_ranking, gold_key

K_VALUES = (1, 3, 5, 10)


@dataclass(frozen=True)
class RetrievalReport:
    rows: list[dict[str, object]]
    summary: dict[str, object]
    by_type: dict[str, dict[str, float]]


def evaluate_retrieval(
    items: Sequence[EvalItem],
    retrieved: Mapping[str, Sequence[RetrievedChunk]],
    latencies_ms: Mapping[str, float] | None = None,
) -> RetrievalReport:
    latencies_ms = latencies_ms or {}
    rows: list[dict[str, object]] = []
    ranked: list[list[str]] = []
    gold: list[str] = []
    types: list[str] = []

    for item in items:
        chunks = list(retrieved.get(item.id, []))
        canonical = canonical_ranking(item, chunks)
        top = chunks[0] if chunks else None
        row: dict[str, object] = {
            "id": item.id,
            "type": item.type,
            "split": item.split,
            "in_corpus": item.in_corpus,
            "question": item.question,
            "top1_distance": top.distance if top else None,
            "top1_material": top.material if top else "",
            "top1_section": " > ".join(top.heading_path) if top else "",
            "found": len(chunks),
            "latency_ms": latencies_ms.get(item.id),
        }
        if item.in_corpus:
            rank = gold_ranks([canonical], [gold_key(item)])[0]
            row["gold_rank"] = rank
            row["rr"] = 0.0 if rank is None else 1.0 / rank
            for k in K_VALUES:
                row[f"hit@{k}"] = int(rank is not None and rank <= k)
            ranked.append(canonical)
            gold.append(gold_key(item))
            types.append(item.type)
        rows.append(row)

    rr_values = reciprocal_ranks(ranked, gold)
    ci_low, ci_high = bootstrap_ci(rr_values)
    coverage = rank_coverage(ranked, gold, shares=(0.9,))
    latencies = [value for value in latencies_ms.values() if value is not None]

    summary: dict[str, object] = {
        "n": len(gold),
        **{f"hit@{k}": hit_rate_at_k(ranked, gold, k) for k in K_VALUES},
        "mrr": mrr(ranked, gold),
        "mrr_ci_low": ci_low,
        "mrr_ci_high": ci_high,
        "coverage@90%": coverage[0.9],
        "latency_p50_ms": percentile(latencies, 50) if latencies else None,
        "latency_p95_ms": percentile(latencies, 95) if latencies else None,
    }

    grouped: dict[str, tuple[list[list[str]], list[str]]] = defaultdict(
        lambda: ([], [])
    )
    for question_type, ranking, answer in zip(types, ranked, gold, strict=True):
        grouped[question_type][0].append(ranking)
        grouped[question_type][1].append(answer)
    by_type = {
        question_type: {
            "n": float(len(group_gold)),
            "hit@5": hit_rate_at_k(group_ranked, group_gold, 5),
            "mrr": mrr(group_ranked, group_gold),
        }
        for question_type, (group_ranked, group_gold) in sorted(grouped.items())
    }
    return RetrievalReport(rows=rows, summary=summary, by_type=by_type)


def format_report(report: RetrievalReport) -> str:
    """Короткая текстовая сводка для терминала."""
    s = report.summary
    lines = [
        f"Вопросов по корпусу: {s['n']}",
        "  ".join(f"Hit@{k}={s[f'hit@{k}']:.3f}" for k in K_VALUES),
        f"MRR={s['mrr']:.3f}  (95% CI {s['mrr_ci_low']:.3f}–{s['mrr_ci_high']:.3f})",
        f"Чтобы покрыть 90% вопросов, нужно top-{s['coverage@90%'] or '∞'}",
    ]
    if report.by_type:
        lines.append("По типам вопросов:")
        for question_type, values in report.by_type.items():
            lines.append(
                f"  {question_type:<14} n={values['n']:.0f}  "
                f"Hit@5={values['hit@5']:.3f}  MRR={values['mrr']:.3f}"
            )
    return "\n".join(lines)
