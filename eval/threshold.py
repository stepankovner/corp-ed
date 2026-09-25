"""Порог отказа по распределению расстояний (A8, шаги 1–2).

    python -m eval.threshold eval/results/<дата>_<конфиг>_retrieval.csv

Вход — результаты режима retrieval (run_eval или bench --retriever
vector/hybrid) на ЗОЛОТОМ наборе: там есть вопросы вне корпуса. Для
каждого вопроса берётся расстояние до ближайшего чанка (для гибрида —
до лучшего векторного кандидата, колонка best_vector_distance).

Печатает два распределения (из корпуса / вне корпуса), гистограмму и F1
отказа при разных порогах. Это оценка только по поиску. Финальное
значение — по e2e (F1 отказа + правильность): у Битрикс24 по метрикам
поиска оптимальным выходил нулевой порог реранкера, а в сквозном прогоне
лучше отвечала конфигурация с порогом 0.5 — «полурелевантные» чанки
мешают модели, даже когда правильный чанк есть. Поэтому скрипт
предлагает 2–3 кандидата для e2e, а не одно число.

Порог подбирается только на паре query→doc: шкалы расстояний у пар
query→doc и doc→doc различаются примерно вдвое (разведка 13.09).
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from eval.metrics import ThresholdPoint, best_threshold, percentile, threshold_sweep
from eval.results import read_csv, write_csv

BIN_WIDTH = 0.02
HISTOGRAM_WIDTH = 40


def read_distances(
    rows: Sequence[dict[str, str]], column: str
) -> tuple[list[float | None], list[bool]]:
    distances: list[float | None] = []
    in_corpus: list[bool] = []
    for row in rows:
        value = row.get(column, "")
        distances.append(float(value) if value not in ("", "None") else None)
        in_corpus.append(row["in_corpus"].strip().casefold() in {"true", "1"})
    return distances, in_corpus


def candidate_thresholds(distances: Sequence[float | None]) -> list[float]:
    """Все наблюдённые расстояния + сетка 0.30–0.90 с шагом 0.01."""
    observed = {round(d, 4) for d in distances if d is not None}
    grid = {round(0.30 + step / 100, 2) for step in range(61)}
    return sorted(observed | grid)


def histogram(
    distances_in: Sequence[float],
    distances_out: Sequence[float],
    width: float = BIN_WIDTH,
) -> list[str]:
    values = [*distances_in, *distances_out]
    if not values:
        return []
    start = int(min(values) / width) * width
    stop = max(values)
    peak = 1
    bins: list[tuple[float, int, int]] = []
    edge = start
    while edge <= stop + 1e-9:
        count_in = sum(1 for d in distances_in if edge <= d < edge + width)
        count_out = sum(1 for d in distances_out if edge <= d < edge + width)
        bins.append((edge, count_in, count_out))
        peak = max(peak, count_in, count_out)
        edge += width
    scale = HISTOGRAM_WIDTH / peak
    return [
        f"{edge:5.2f} | {'#' * round(n_in * scale):<{HISTOGRAM_WIDTH}} | "
        f"{'o' * round(n_out * scale)}"
        for edge, n_in, n_out in bins
    ]


def describe(values: Sequence[float]) -> str:
    if not values:
        return "нет данных"
    return (
        f"n={len(values)} min={min(values):.3f} p10={percentile(values, 10):.3f} "
        f"p50={percentile(values, 50):.3f} p90={percentile(values, 90):.3f} "
        f"max={max(values):.3f}"
    )


def candidates_for_e2e(points: Sequence[ThresholdPoint]) -> list[ThresholdPoint]:
    """Лучший по F1 и его соседи: строже (−0.05) и мягче (+0.05)."""
    best = best_threshold(points)
    if best is None:
        return []
    chosen = [best]
    for delta in (-0.05, 0.05):
        target = best.threshold + delta
        nearest = min(points, key=lambda p: abs(p.threshold - target))
        if nearest not in chosen:
            chosen.append(nearest)
    return sorted(chosen, key=lambda p: p.threshold)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.threshold")
    parser.add_argument("results", type=Path)
    parser.add_argument(
        "--column",
        default="",
        help="по умолчанию best_vector_distance или top1_distance",
    )
    args = parser.parse_args(argv)

    rows = read_csv(args.results)
    column = args.column or (
        "best_vector_distance"
        if rows and "best_vector_distance" in rows[0]
        else "top1_distance"
    )
    distances, in_corpus = read_distances(rows, column)
    inside = [
        d for d, c in zip(distances, in_corpus, strict=True) if c and d is not None
    ]
    outside = [
        d for d, c in zip(distances, in_corpus, strict=True) if not c and d is not None
    ]

    print(f"Колонка: {column}")
    print(f"Из корпуса:  {describe(inside)}")
    print(f"Вне корпуса: {describe(outside)}")
    if not outside:
        print("Нет вопросов вне корпуса — порог отказа по этому набору не подобрать.")
        return 1
    print("\nГистограмма (# — из корпуса, o — вне корпуса):")
    print("\n".join(histogram(inside, outside)))

    points = threshold_sweep(distances, in_corpus, candidate_thresholds(distances))
    sweep_path = args.results.with_name(args.results.stem + "_threshold_sweep.csv")
    write_csv(
        sweep_path,
        [
            {
                "threshold": p.threshold,
                "precision": p.precision,
                "recall": p.recall,
                "f1": p.f1,
                "answered_share": p.answered_share,
            }
            for p in points
        ],
    )

    print("\nКандидаты для e2e-прогона (финальный выбор — по e2e, A8.3):")
    for point in candidates_for_e2e(points):
        print(
            f"  max_distance={point.threshold:.3f}: precision={point.precision:.3f} "
            f"recall={point.recall:.3f} F1={point.f1:.3f} "
            f"отвечает на {point.answered_share:.0%} вопросов"
        )
    if inside and outside and max(inside) < min(outside):
        gap = (max(inside) + min(outside)) / 2
        print(f"Распределения не пересекаются: середина зазора {gap:.3f}.")
    print(f"Полная таблица: {sweep_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
