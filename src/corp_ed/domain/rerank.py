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

Длинные «разговорные» вопросы модель путает: выталкивает из пятёрки
фрагмент, который вектор ставит первым (`ml-report.md`, «Реранкер и BH-37
вместе»). Починка — `rerank_allowed`: реранкер только на вопросах не
длиннее `RERANK_MAX_WORDS` слов (BH-40; «Реранкер: починка длинных
вопросов»). `order` keep_first и rrf замерены и не выбраны — для замеров.
"""

from collections.abc import Callable, Hashable, Mapping, Sequence
from typing import Literal

RerankOrder = Literal["score", "keep_first", "rrf"]
"""Как ставить прошедших порог: score — по баллу модели; keep_first —
ближайший по вектору первым, остальные по баллу; rrf — по сумме
1 / (rrf_k + место) по вектору и по баллу."""

RRF_K = 60
RERANK_MAX_WORDS = 24
"""Самый длинный вопрос dev и holdout, на которых прирост реранкера
подтверждён; длиннее — реранкер вредит (замер 04.10)."""


def order_by_scores[T: Hashable](
    candidates: Sequence[T], scores: Sequence[float]
) -> list[T]:
    """Кандидаты по убыванию балла; при равных баллах — исходный порядок."""
    if len(candidates) != len(scores):
        raise ValueError("candidates и scores разной длины")
    order = sorted(range(len(candidates)), key=lambda i: (-scores[i], i))
    return [candidates[i] for i in order]


def order_passed[T: Hashable](
    passed: Sequence[T],
    scores: Sequence[float],
    order: RerankOrder = "score",
    rrf_k: int = RRF_K,
) -> list[T]:
    """Прошедшие порог (в порядке вектора) — в порядке ответа по правилу order."""
    by_score = order_by_scores(passed, scores)
    if order == "score" or not passed:
        return by_score
    if order == "keep_first":
        return [passed[0], *(item for item in by_score if item != passed[0])]
    if order == "rrf":
        place = {item: n for n, item in enumerate(by_score)}
        fused = [
            1 / (rrf_k + n + 1) + 1 / (rrf_k + place[item] + 1)
            for n, item in enumerate(passed)
        ]
        return order_by_scores(passed, fused)
    raise ValueError(f"неизвестный order: {order}")


def rerank_allowed(question: str, max_words: int | None) -> bool:
    """Звать ли реранкер: вопрос не длиннее max_words слов (None — всегда)."""
    return max_words is None or len(question.split()) <= max_words


def rerank[T: Hashable](
    ranking: Sequence[T],
    distance_of: Mapping[T, float],
    score: Callable[[list[T]], Sequence[float]],
    *,
    depth: int,
    max_distance: float | None = None,
    order: RerankOrder = "score",
) -> list[T]:
    """Переставить первые `depth` кандидатов по баллам; остальные — как были.

    `score` получает только прошедших порог (`distance ≤ max_distance`) и
    возвращает их баллы в том же порядке. `max_distance=None` — оценивать
    всех первых `depth`. Если порог не прошёл никто, `score` не вызывается.
    `order` — как ставить прошедших (`order_passed`).
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
    ordered = order_passed(passed, score(passed), order)
    return ordered + rest + list(ranking[depth:])
