"""Реранкер (M3): какие кандидаты отдать кросс-энкодеру и как переставить выдачу.

Чистые функции без модели. Модель вызывает бэкенд (BH-32), здесь — правило,
которое замерено на стенде (`eval/rerank.py`; `ml-report.md`, «M3» и
«M3 вариант б»):

1. вектор отдаёт первые `depth` кандидатов (замерено при 30);
2. кросс-энкодер оценивает только тех, кто прошёл порог отказа;
3. они идут первыми, по убыванию балла; остальные — следом, в векторном
   порядке.

Порог по-прежнему решается по вектору: реранкер выбирает, какие из
прошедших порог фрагментов попадут в ответ, но не решает, отвечать ли.
Пусто после порога — выдача без изменений, модель не вызывается.

Текст пары для модели — `embed_text` фрагмента (крошки «Документ > Раздел»
и текст): на нём замерено качество. Модель — `mmarco-mMiniLMv2-L12-H384-v1`:
на золотом dev то же качество, что у bge-reranker-v2-m3, за 1,8 с на
вопрос на 4 vCPU (30 кандидатов).
"""

from collections.abc import Callable, Hashable, Mapping, Sequence


def order_by_scores[T: Hashable](
    candidates: Sequence[T], scores: Sequence[float]
) -> list[T]:
    """Кандидаты по убыванию балла; при равных баллах — исходный порядок."""
    if len(candidates) != len(scores):
        raise ValueError("candidates и scores разной длины")
    order = sorted(range(len(candidates)), key=lambda i: (-scores[i], i))
    return [candidates[i] for i in order]


def rerank[T: Hashable](
    ranking: Sequence[T],
    distance_of: Mapping[T, float],
    score: Callable[[list[T]], Sequence[float]],
    *,
    depth: int,
    max_distance: float | None = None,
) -> list[T]:
    """Переставить первые `depth` кандидатов по баллам; остальные — как были.

    `score` получает только прошедших порог (`distance ≤ max_distance`) и
    возвращает их баллы в том же порядке. `max_distance=None` — оценивать
    всех первых `depth`. Если порог не прошёл никто, `score` не вызывается.
    """
    if depth <= 0:
        raise ValueError("depth должен быть больше нуля")
    head = list(ranking[:depth])
    passed = [
        item
        for item in head
        if max_distance is None
        or (item in distance_of and distance_of[item] <= max_distance)
    ]
    if not passed:
        return list(ranking)
    passed_set = set(passed)
    rest = [item for item in head if item not in passed_set]
    return order_by_scores(passed, score(passed)) + rest + list(ranking[depth:])
