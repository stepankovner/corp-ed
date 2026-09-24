"""Сравнить два прогона поиска на одних и тех же вопросах.

    python -m eval.compare eval/results/A.csv eval/results/B.csv [--metric rr]

Метрика по умолчанию — rr (1/ранг, среднее = MRR); можно hit@1, hit@5 и т. д.
Печатает разницу B − A, её 95% доверительный интервал (бутстреп по
вопросам) и p-value парного перестановочного теста. Решение «B лучше» —
только если интервал не содержит ноль (как у Битрикс24). Каждый
эксперимент меняет одну вещь (ТЗ, A7).
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from eval.metrics import bootstrap_ci, paired_permutation_test
from eval.results import read_csv

ALPHA = 0.05


def paired_values(
    rows_a: Sequence[dict[str, str]], rows_b: Sequence[dict[str, str]], metric: str
) -> tuple[list[str], list[float], list[float]]:
    """Значения метрики по вопросам, которые есть в обоих прогонах."""
    b_by_id = {row["id"]: row for row in rows_b if row.get(metric, "") != ""}
    ids: list[str] = []
    a_values: list[float] = []
    b_values: list[float] = []
    for row in rows_a:
        if row.get(metric, "") == "" or row["id"] not in b_by_id:
            continue
        ids.append(row["id"])
        a_values.append(float(row[metric]))
        b_values.append(float(b_by_id[row["id"]][metric]))
    return ids, a_values, b_values


def verdict(low: float, high: float, p_value: float, alpha: float = ALPHA) -> str:
    """Решение по разнице B − A: оба критерия должны согласиться.

    Интервал бутстрепа на малой выборке бывает на волосок от нуля, когда
    перестановочный тест говорит «не значимо» (так было на демо-наборе
    из 23 вопросов: CI [+0.0001; +0.149], p = 0.12). Поэтому «лучше» —
    только если интервал не содержит ноль И p < alpha, как у Битрикс24.
    """
    if low > 0 and p_value < alpha:
        return "B лучше A, разница вне шума."
    if high < 0 and p_value < alpha:
        return "B хуже A, разница вне шума."
    return "разница в пределах шума — по этим данным конфигурации не различить."


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.compare")
    parser.add_argument("a", type=Path)
    parser.add_argument("b", type=Path)
    parser.add_argument("--metric", default="rr")
    args = parser.parse_args(argv)

    ids, a, b = paired_values(read_csv(args.a), read_csv(args.b), args.metric)
    if not ids:
        print("Нет общих вопросов с этой метрикой.")
        return 1

    diffs = [y - x for x, y in zip(a, b, strict=True)]
    mean_a, mean_b = sum(a) / len(a), sum(b) / len(b)
    low, high = bootstrap_ci(diffs)
    p_value = paired_permutation_test(b, a)

    print(f"Вопросов: {len(ids)}, метрика: {args.metric}")
    print(f"A = {mean_a:.4f}  ({args.a.name})")
    print(f"B = {mean_b:.4f}  ({args.b.name})")
    print(
        f"B − A = {mean_b - mean_a:+.4f}, "
        f"95% CI [{low:+.4f}; {high:+.4f}], p = {p_value:.4f}"
    )
    print(f"Вывод: {verdict(low, high, p_value)}")
    better = sum(d > 0 for d in diffs)
    worse = sum(d < 0 for d in diffs)
    print(
        f"Вопросов, где B лучше: {better}, хуже: {worse}, "
        f"без изменений: {len(ids) - better - worse}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
