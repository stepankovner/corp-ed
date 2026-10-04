"""Порог отказа с разведёнными ролями (BH-37, замер ML 04.10)."""

import pytest

from corp_ed.domain.threshold import relevance_limit


@pytest.mark.parametrize(
    ("nearest", "expected"),
    [
        (None, None),  # выдачи нет
        (0.41, 0.59),  # ближе порога — как раньше
        (0.59, 0.59),
        (0.62, pytest.approx(0.67)),  # в зоне — ближайшая + запас
        (0.70, pytest.approx(0.75)),
        (0.71, None),  # дальше gate — ответа по документам нет
    ],
)
def test_relevance_limit_with_gate(nearest: float | None, expected: object) -> None:
    assert (
        relevance_limit(nearest, 0.59, gate_distance=0.70, near_margin=0.05) == expected
    )


def test_without_gate_behaves_as_before() -> None:
    assert relevance_limit(0.5, 0.59) == 0.59
    assert relevance_limit(0.6, 0.59) is None
    assert relevance_limit(None, 0.59) is None
