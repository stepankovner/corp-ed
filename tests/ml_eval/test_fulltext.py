"""Покрытие лексем вопроса и подобие ts_rank_cd (BH-51 (а), замер 23.10)."""

import pytest

from corp_ed.domain.fulltext import lexeme_coverage
from eval.bm25 import BM25Index, tokenize


def test_coverage_is_share_of_question_lexemes_in_fragment() -> None:
    assert lexeme_coverage(["отпуск", "дн", "скольк"], {"отпуск", "дн", "28"}) == (
        pytest.approx(2 / 3)
    )
    # Повторы в вопросе не важны: лексемы — множество, как tsvector_to_array.
    assert lexeme_coverage(["отпуск", "отпуск"], {"отпуск"}) == 1.0
    # Вопрос без лексем (знаки, стоп-слова) — искать нечем.
    assert lexeme_coverage([], {"отпуск"}) == 0.0
    assert lexeme_coverage(tokenize("а и в?"), {"отпуск"}) == 0.0


def test_cover_density_counts_occurrences_without_idf_or_length() -> None:
    index = BM25Index(
        [
            "отпуск отпуск отпуск премия премия премия",  # частое слово трижды
            "отпуск компенсация",  # редкое слово — BM25 любит его больше
            "погода",
        ]
    )

    cd = index.cover_density_scores("отпуск и компенсация")
    assert cd == pytest.approx([0.3, 0.2, 0.0])
    # BM25 ставит выше редкое слово, подобие ts_rank_cd — число вхождений.
    assert index.search("отпуск и компенсация", 2)[0][0] == 1
    assert index.search("отпуск и компенсация", 2, rank="cd")[0][0] == 0
    assert index.lexemes(1) == set(tokenize("отпуск компенсация"))
