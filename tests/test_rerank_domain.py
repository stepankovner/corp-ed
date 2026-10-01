"""Реранкер в домене (M3, BH-32): порядок кандидатов по баллам."""

import pytest

from corp_ed.domain.rerank import order_by_scores, rerank


def test_order_by_scores_keeps_original_order_on_ties() -> None:
    assert order_by_scores(["a", "b", "c"], [0.2, 0.9, 0.2]) == ["b", "a", "c"]
    with pytest.raises(ValueError):
        order_by_scores(["a"], [0.1, 0.2])


def test_rerank_scores_only_candidates_that_passed_threshold() -> None:
    ranking = ["c1", "c2", "c3", "c4", "c5"]
    distance_of = {"c1": 0.30, "c2": 0.62, "c3": 0.41, "c4": 0.50, "c5": 0.20}
    seen: list[list[str]] = []

    def score(items: list[str]) -> list[float]:
        seen.append(items)
        return [{"c1": 0.1, "c3": 0.8, "c4": 0.5}[item] for item in items]

    result = rerank(ranking, distance_of, score, depth=4, max_distance=0.59)

    # c2 за порогом — модели не показываем; c5 за пределами depth — в хвосте.
    assert seen == [["c1", "c3", "c4"]]
    assert result == ["c3", "c4", "c1", "c2", "c5"]


def test_rerank_without_passed_candidates_keeps_ranking_and_skips_model() -> None:
    def score(items: list[str]) -> list[float]:
        raise AssertionError("модель не должна вызываться")

    ranking = ["a", "b"]
    assert (
        rerank(ranking, {"a": 0.7, "b": 0.8}, score, depth=2, max_distance=0.59)
        == ranking
    )


def test_rerank_without_threshold_scores_whole_head() -> None:
    result = rerank([1, 2, 3], {}, lambda items: [float(i) for i in items], depth=3)
    assert result == [3, 2, 1]
    with pytest.raises(ValueError):
        rerank([1], {}, lambda items: [0.0], depth=0)
