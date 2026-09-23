"""Метрики eval — чистые функции без сети и файлов.

Контракт ТЗ (A6):
    hit_rate_at_k(ranked, gold, k), mrr(ranked, gold), refusal_prf(answered, in_corpus)

ranked[i] — список id, которые поиск вернул на вопрос i, по убыванию
релевантности; gold[i] — id правильного ответа. Когда правильных чанков
несколько (перекрытие, соседние чанки), вызывающий код заменяет id всех
релевантных чанков на один канонический gold-id (см. eval/relevance.py) —
тогда эти функции работают без изменений.

Сверх контракта (идеи Битрикс24, «Источники» в ТЗ):
- bootstrap_ci и paired_permutation_test: разница двух конфигураций на
  150–200 вопросах в 2–3 п. п. может быть шумом; Битрикс24 принимал
  изменение, только если 95% доверительный интервал разницы не содержит
  ноль и перестановочный тест даёт малое p;
- rank_coverage («воронка»): сколько кандидатов нужно показать, чтобы
  покрыть 90/95/99% вопросов — прямой ответ на вопрос E4 (faq_limit);
- threshold_sweep: F1 отказа при разных порогах расстояния (A8).
"""

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass


def _check_lengths(*sequences: Sequence[object]) -> None:
    if len({len(sequence) for sequence in sequences}) > 1:
        raise ValueError("all inputs must have the same length")


def gold_ranks(
    ranked: Sequence[Sequence[str]], gold: Sequence[str]
) -> list[int | None]:
    """Позиция (с 1) правильного ответа в выдаче; None — не найден."""
    _check_lengths(ranked, gold)
    ranks: list[int | None] = []
    for items, answer in zip(ranked, gold, strict=True):
        try:
            ranks.append(list(items).index(answer) + 1)
        except ValueError:
            ranks.append(None)
    return ranks


def hits_at_k(
    ranked: Sequence[Sequence[str]], gold: Sequence[str], k: int
) -> list[float]:
    """Для каждого вопроса: 1.0, если ответ в top-k, иначе 0.0."""
    if k <= 0:
        raise ValueError("k must be positive")
    return [
        1.0 if rank is not None and rank <= k else 0.0
        for rank in gold_ranks(ranked, gold)
    ]


def reciprocal_ranks(
    ranked: Sequence[Sequence[str]], gold: Sequence[str]
) -> list[float]:
    """Для каждого вопроса: 1 / ранг ответа, 0.0 если ответа нет в выдаче."""
    return [0.0 if rank is None else 1.0 / rank for rank in gold_ranks(ranked, gold)]


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def hit_rate_at_k(ranked: list[list[str]], gold: list[str], k: int) -> float:
    """Доля вопросов, у которых правильный ответ в первых k результатах."""
    return _mean(hits_at_k(ranked, gold, k))


def mrr(ranked: list[list[str]], gold: list[str]) -> float:
    """Mean Reciprocal Rank: среднее 1 / ранг правильного ответа."""
    return _mean(reciprocal_ranks(ranked, gold))


def refusal_prf(
    answered: list[bool], in_corpus: list[bool]
) -> tuple[float, float, float]:
    """Precision, recall, F1 классификации «ответил / отказал».

    Положительный класс — «вопрос из корпуса, ассистент ответил» (как
    у Битрикс24): precision — доля вопросов из корпуса среди тех, на
    которые ассистент ответил; recall — доля вопросов из корпуса, на которые
    он ответил. Правильность самого ответа здесь не учитывается — она
    меряется отдельно.
    """
    _check_lengths(answered, in_corpus)
    tp = sum(1 for a, c in zip(answered, in_corpus, strict=True) if a and c)
    fp = sum(1 for a, c in zip(answered, in_corpus, strict=True) if a and not c)
    fn = sum(1 for a, c in zip(answered, in_corpus, strict=True) if not a and c)

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def rank_coverage(
    ranked: Sequence[Sequence[str]],
    gold: Sequence[str],
    shares: Sequence[float] = (0.5, 0.9, 0.95, 0.99),
) -> dict[float, int | None]:
    """«Воронка»: минимальный K, при котором доля вопросов с ответом в top-K ≥ share.

    None — такую долю не покрыть никаким K (ответ не найден у слишком
    многих вопросов).
    """
    ranks = gold_ranks(ranked, gold)
    if not ranks:
        return dict.fromkeys(shares)
    ordered = sorted(math.inf if rank is None else rank for rank in ranks)
    result: dict[float, int | None] = {}
    for share in shares:
        index = max(0, math.ceil(share * len(ordered)) - 1)
        value = ordered[index]
        result[share] = None if value == math.inf else int(value)
    return result


def bootstrap_ci(
    values: Sequence[float],
    *,
    iterations: int = 2000,
    confidence: float = 0.95,
    seed: int = 0,
) -> tuple[float, float]:
    """Доверительный интервал среднего бутстрепом (перцентильный метод)."""
    if not values:
        return 0.0, 0.0
    rng = random.Random(seed)
    n = len(values)
    means = sorted(
        _mean([values[rng.randrange(n)] for _ in range(n)]) for _ in range(iterations)
    )
    alpha = (1 - confidence) / 2
    low = means[int(alpha * (iterations - 1))]
    high = means[int((1 - alpha) * (iterations - 1))]
    return low, high


def paired_permutation_test(
    a: Sequence[float],
    b: Sequence[float],
    *,
    iterations: int = 10000,
    seed: int = 0,
) -> float:
    """Двусторонний p-value для разницы средних двух конфигураций на одних вопросах.

    a[i] и b[i] — метрика одного и того же вопроса (например, 1/ранг) в
    конфигурациях A и B. Под нулевой гипотезой знак каждой разницы
    случаен; p — доля случайных перестановок знаков, давших разницу не
    меньше наблюдаемой. Малое p (< 0.05) — разница вряд ли случайна.
    """
    _check_lengths(a, b)
    diffs = [x - y for x, y in zip(a, b, strict=True)]
    if not diffs:
        return 1.0
    observed = abs(_mean(diffs))
    rng = random.Random(seed)
    extreme = 0
    for _ in range(iterations):
        permuted = _mean([d if rng.random() < 0.5 else -d for d in diffs])
        if abs(permuted) >= observed - 1e-12:
            extreme += 1
    return (extreme + 1) / (iterations + 1)


def percentile(values: Sequence[float], q: float) -> float:
    """Перцентиль q ∈ [0, 100] с линейной интерполяцией (как numpy по умолчанию)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * q / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


@dataclass(frozen=True)
class ThresholdPoint:
    threshold: float
    precision: float
    recall: float
    f1: float
    answered_share: float


def threshold_sweep(
    distances: Sequence[float | None],
    in_corpus: Sequence[bool],
    thresholds: Sequence[float],
) -> list[ThresholdPoint]:
    """F1 отказа при разных порогах max_distance (A8).

    distances[i] — расстояние до ближайшего чанка для вопроса i (None —
    поиск ничего не вернул). Ассистент «отвечает», если distance ≤ порога.
    Это оценка только по поиску: модель ещё может отказать сама, поэтому
    финальный порог выбирается по e2e (урок Битрикс24).
    """
    _check_lengths(distances, in_corpus)
    points: list[ThresholdPoint] = []
    for threshold in thresholds:
        answered = [d is not None and d <= threshold for d in distances]
        precision, recall, f1 = refusal_prf(answered, list(in_corpus))
        share = sum(answered) / len(answered) if answered else 0.0
        points.append(ThresholdPoint(threshold, precision, recall, f1, share))
    return points


def best_threshold(points: Sequence[ThresholdPoint]) -> ThresholdPoint | None:
    """Порог с максимальным F1; при равенстве — меньший (строже к отказу)."""
    if not points:
        return None
    return max(points, key=lambda point: (point.f1, -point.threshold))
