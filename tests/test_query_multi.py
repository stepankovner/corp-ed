import pytest

from corp_ed.domain.query import fuse_query_rankings


def test_original_only_keeps_order() -> None:
    assert fuse_query_rankings(["a", "b", "c"], []) == ["a", "b", "c"]


def test_chunk_found_by_every_query_rises() -> None:
    fused = fuse_query_rankings(["a", "b", "c"], [["b", "d"], ["b", "e"]])

    assert fused[0] == "b"
    # Найденное только переформулировкой — в выдаче, но после общих.
    assert set(fused) == {"a", "b", "c", "d", "e"}
    assert fused.index("a") < fused.index("d")


def test_paraphrases_with_zero_weight_are_ignored() -> None:
    fused = fuse_query_rankings(["a", "b"], [["b", "c"]], paraphrase_weight=0.0)

    assert fused[:2] == ["a", "b"]


def test_original_wins_ties() -> None:
    # Одинаковые ранги: у исходного вопроса вес 1.0, у переформулировки 0.5.
    assert fuse_query_rankings(["a"], [["b"]], paraphrase_weight=0.5) == ["a", "b"]


def test_negative_weight_is_rejected() -> None:
    with pytest.raises(ValueError):
        fuse_query_rankings(["a"], [["b"]], paraphrase_weight=-1.0)
