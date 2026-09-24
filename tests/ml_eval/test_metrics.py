import pytest

from eval.metrics import (
    ThresholdPoint,
    best_threshold,
    bootstrap_ci,
    gold_ranks,
    hit_rate_at_k,
    mrr,
    paired_permutation_test,
    percentile,
    rank_coverage,
    refusal_prf,
    threshold_sweep,
)

RANKED = [["g1", "x", "y"], ["x", "g2", "y"], ["x", "y", "z"], ["x", "y", "g4"]]
GOLD = ["g1", "g2", "g3", "g4"]


def test_gold_ranks() -> None:
    assert gold_ranks(RANKED, GOLD) == [1, 2, None, 3]


@pytest.mark.parametrize(
    ("k", "expected"), [(1, 0.25), (2, 0.5), (3, 0.75), (10, 0.75)]
)
def test_hit_rate_at_k(k: int, expected: float) -> None:
    assert hit_rate_at_k(RANKED, GOLD, k) == pytest.approx(expected)


def test_mrr() -> None:
    assert mrr(RANKED, GOLD) == pytest.approx((1 + 1 / 2 + 0 + 1 / 3) / 4)


def test_empty_inputs() -> None:
    assert hit_rate_at_k([], [], 5) == 0.0
    assert mrr([], []) == 0.0
    assert refusal_prf([], []) == (0.0, 0.0, 0.0)


def test_single_element() -> None:
    assert hit_rate_at_k([["a"]], ["a"], 1) == 1.0
    assert mrr([["b", "a"]], ["a"]) == 0.5


def test_empty_ranking_for_question() -> None:
    assert mrr([[]], ["a"]) == 0.0


def test_gold_counted_once_at_best_rank() -> None:
    assert mrr([["x", "g", "g"]], ["g"]) == pytest.approx(0.5)


def test_length_mismatch() -> None:
    with pytest.raises(ValueError):
        mrr([["a"]], ["a", "b"])


def test_k_must_be_positive() -> None:
    with pytest.raises(ValueError):
        hit_rate_at_k(RANKED, GOLD, 0)


def test_refusal_prf() -> None:
    #           ответил  из корпуса
    answered = [True, True, True, False, False, True]
    in_corpus = [True, True, False, True, False, False]

    precision, recall, f1 = refusal_prf(answered, in_corpus)

    assert precision == pytest.approx(2 / 4)
    assert recall == pytest.approx(2 / 3)
    assert f1 == pytest.approx(2 * 0.5 * (2 / 3) / (0.5 + 2 / 3))


def test_refusal_prf_like_bitrix_old_agent() -> None:
    # Отвечает точно, но редко: высокая precision, низкий recall.
    answered = [True] * 5 + [False] * 5 + [False] * 5
    in_corpus = [True] * 10 + [False] * 5

    precision, recall, _ = refusal_prf(answered, in_corpus)

    assert (precision, recall) == (1.0, 0.5)


def test_refusal_prf_nothing_answered() -> None:
    assert refusal_prf([False, False], [True, False]) == (0.0, 0.0, 0.0)


def test_rank_coverage() -> None:
    ranked = [["g"], ["x", "g"], ["x", "x", "x", "g"], ["x"]]
    gold = ["g"] * 4

    assert rank_coverage(ranked, gold, shares=(0.25, 0.5, 0.75, 1.0)) == {
        0.25: 1,
        0.5: 2,
        0.75: 4,
        1.0: None,
    }


def test_rank_coverage_empty() -> None:
    assert rank_coverage([], [], shares=(0.9,)) == {0.9: None}


def test_bootstrap_ci_contains_mean_and_is_deterministic() -> None:
    values = [0.0, 1.0] * 50

    low, high = bootstrap_ci(values, seed=1)

    assert low < 0.5 < high
    assert bootstrap_ci(values, seed=1) == (low, high)


def test_bootstrap_ci_constant_values() -> None:
    assert bootstrap_ci([0.7] * 20) == pytest.approx((0.7, 0.7))
    assert bootstrap_ci([]) == (0.0, 0.0)


def test_permutation_test_identical_configs() -> None:
    values = [1.0, 0.5, 0.0, 0.33] * 10

    assert paired_permutation_test(values, values) == pytest.approx(1.0)


def test_permutation_test_clear_difference() -> None:
    better = [1.0] * 40
    worse = [0.0] * 40

    assert paired_permutation_test(better, worse, iterations=2000) < 0.01


def test_permutation_test_noise_is_not_significant() -> None:
    a = [1.0, 0.0] * 20
    b = [0.0, 1.0] * 20

    assert paired_permutation_test(a, b, iterations=2000) > 0.5


@pytest.mark.parametrize(
    ("q", "expected"), [(0, 1.0), (50, 2.5), (95, 3.85), (100, 4.0)]
)
def test_percentile_matches_numpy_linear(q: float, expected: float) -> None:
    assert percentile([4.0, 1.0, 3.0, 2.0], q) == pytest.approx(expected)


def test_percentile_empty() -> None:
    assert percentile([], 50) == 0.0


def test_threshold_sweep() -> None:
    distances = [0.40, 0.50, 0.65, 0.55, 0.75, None]
    in_corpus = [True, True, True, False, False, False]

    points = threshold_sweep(distances, in_corpus, [0.45, 0.6, 0.7])

    assert points[0] == ThresholdPoint(0.45, 1.0, 1 / 3, 0.5, 1 / 6)
    assert points[1].precision == pytest.approx(2 / 3)
    assert points[2].recall == 1.0
    assert best_threshold(points) == points[2]


def test_best_threshold_prefers_stricter_on_tie() -> None:
    points = [ThresholdPoint(0.7, 1, 1, 1, 0.5), ThresholdPoint(0.6, 1, 1, 1, 0.5)]

    best = best_threshold(points)

    assert best is not None and best.threshold == 0.6
    assert best_threshold([]) is None
