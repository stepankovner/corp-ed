"""Реранкер в домене (M3, BH-32): порядок кандидатов по баллам."""

import pytest

from corp_ed.domain.rerank import order_by_scores, order_passed, rerank, rerank_allowed


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


def test_order_passed_rules_for_long_questions() -> None:
    # Прошедшие порог — в порядке вектора; модель любит «c» и «b», а
    # ближайший по вектору «a» ставит последним (как на длинных вопросах).
    passed = ["a", "b", "c", "d"]
    scores = [0.1, 0.8, 0.9, 0.2]

    assert order_passed(passed, scores) == ["c", "b", "d", "a"]
    assert order_passed(passed, scores, "keep_first") == ["a", "c", "b", "d"]
    # RRF, k = 60: места по вектору a1 b2 c3 d4, по баллу c1 b2 d3 a4 —
    # c 1/63 + 1/61 > b 2/62 > a 1/61 + 1/64 > d 1/64 + 1/63; «a» уже не
    # последний, как при сортировке по баллу.
    assert order_passed(passed, scores, "rrf") == ["c", "b", "a", "d"]
    assert order_passed([], [], "keep_first") == []
    with pytest.raises(ValueError):
        order_passed(passed, scores, "other")  # type: ignore[arg-type]


def test_rerank_order_applies_only_to_passed() -> None:
    ranking = ["a", "far", "b", "c"]
    distance_of = {"a": 0.3, "far": 0.7, "b": 0.4, "c": 0.5}
    scores = {"a": 0.1, "b": 0.5, "c": 0.9}

    def score(items: list[str]) -> list[float]:
        return [scores[item] for item in items]

    kept = rerank(
        ranking, distance_of, score, depth=4, max_distance=0.59, order="keep_first"
    )
    assert kept == ["a", "c", "b", "far"]


def test_rerank_allowed_counts_words() -> None:
    assert rerank_allowed("Сколько дней отпуска?", 40)
    assert not rerank_allowed(" ".join(["слово"] * 41), 40)
    assert rerank_allowed(" ".join(["слово"] * 40), 40)
    assert rerank_allowed(" ".join(["слово"] * 400), None)
