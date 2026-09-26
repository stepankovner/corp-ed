"""Слияние выдач нескольких поисков — Reciprocal Rank Fusion (M1).

Гибрид «вектор + полнотекстовый» — самое подтверждённое улучшение
в статьях: у Битрикс24 +6,6 п. п. Recall@10 против чистого вектора,
у МТС это второй шаг цепочки. Стартовые параметры — оптимум Битрикс24:
k = 60, вес вектора 1.0, вес полнотекстовой ветки 0.5 (вес выше 1.0
ухудшал качество).

Порог отказа по скору RRF ставить НЕЛЬЗЯ. Скор зависит только от рангов,
а не от близости: лучший кандидат получает ~1/61 и когда он точный ответ,
и когда вся выдача — мусор. У МТС скоры RRF «схлопывались» так, что
порядок решали поправки ±0,05. Порог остаётся на расстоянии лучшего
векторного кандидата (или на скоре реранкера, если он будет — M3).
"""

from collections.abc import Hashable, Sequence

DEFAULT_RRF_K = 60


def rrf_merge[T: Hashable](
    rankings: Sequence[Sequence[T]],
    weights: Sequence[float],
    k: int = DEFAULT_RRF_K,
) -> list[tuple[T, float]]:
    """Объединить ранжированные списки id в один.

    score(id) = Σ weight_i / (k + rank_i), rank с 1. id, которого нет
    в списке i, не получает слагаемого от этого списка. Если id повторяется
    внутри одного списка, учитывается лучшая (первая) позиция.

    Порядок при равных скорах — детерминированный: кто раньше встретился
    при обходе списков по порядку (первый список — самый доверенный,
    обычно вектор).
    """
    if len(rankings) != len(weights):
        raise ValueError("rankings and weights must have the same length")
    if k < 0:
        raise ValueError("k must be non-negative")

    scores: dict[T, float] = {}
    for ranking, weight in zip(rankings, weights, strict=True):
        seen: set[T] = set()
        for rank, item in enumerate(ranking, start=1):
            if item in seen:
                continue
            seen.add(item)
            scores[item] = scores.get(item, 0.0) + weight / (k + rank)

    # dict хранит порядок первого появления, sorted стабилен — это и есть
    # правило для равных скоров.
    return sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
