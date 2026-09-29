"""Тесты MVP-функций поиска: RRF (M1), полнотекстовый запрос (M1), словарь (M5)."""

import pytest

from corp_ed.domain.fulltext import to_fulltext_query
from corp_ed.domain.fusion import rrf_merge
from corp_ed.domain.query import expand_query

# --- rrf_merge ---------------------------------------------------------------------


def test_rrf_formula_with_weights() -> None:
    merged = dict(rrf_merge([["a", "b"], ["b", "c"]], weights=[1.0, 0.5], k=60))

    assert merged["a"] == pytest.approx(1 / 61)
    assert merged["b"] == pytest.approx(1 / 62 + 0.5 / 61)
    assert merged["c"] == pytest.approx(0.5 / 62)


def test_rrf_order() -> None:
    merged = rrf_merge([["a", "b", "c"], ["c", "b", "a"]], weights=[1.0, 1.0], k=60)

    # b на втором месте в обоих списках, a и c — 1-е и 3-е места: 1/61+1/63 > 2/62.
    assert [item for item, _ in merged] == ["a", "c", "b"]


def test_rrf_item_missing_in_one_ranking_gets_no_term() -> None:
    merged = dict(rrf_merge([["a"], ["b"]], weights=[1.0, 0.5]))

    assert merged == {"a": pytest.approx(1 / 61), "b": pytest.approx(0.5 / 61)}


def test_rrf_duplicate_inside_ranking_counts_best_rank_once() -> None:
    merged = dict(rrf_merge([["a", "a", "b"]], weights=[1.0], k=0))

    assert merged == {"a": pytest.approx(1.0), "b": pytest.approx(1 / 3)}


def test_rrf_ties_keep_first_appearance() -> None:
    merged = rrf_merge([["vec"], ["fts"]], weights=[1.0, 1.0])

    assert [item for item, _ in merged] == ["vec", "fts"]


def test_rrf_empty() -> None:
    assert rrf_merge([], weights=[]) == []
    assert rrf_merge([[], []], weights=[1.0, 0.5]) == []


def test_rrf_single_ranking_keeps_order() -> None:
    assert [i for i, _ in rrf_merge([["x", "y", "z"]], weights=[1.0])] == [
        "x",
        "y",
        "z",
    ]


def test_rrf_validation() -> None:
    with pytest.raises(ValueError):
        rrf_merge([["a"]], weights=[1.0, 0.5])
    with pytest.raises(ValueError):
        rrf_merge([["a"]], weights=[1.0], k=-1)


def test_rrf_works_with_uuid_like_ids() -> None:
    from uuid import uuid4

    first, second = uuid4(), uuid4()

    assert [i for i, _ in rrf_merge([[first, second]], weights=[1.0])] == [
        first,
        second,
    ]


# --- to_fulltext_query ---------------------------------------------------------------


def test_fulltext_query_joins_words_with_or() -> None:
    assert to_fulltext_query("Сколько дней отпуска положено?") == (
        "Сколько or дней or отпуска or положено"
    )


def test_fulltext_query_drops_operators_and_punctuation() -> None:
    query = to_fulltext_query('Можно "перенести" -отпуск or больничный, и т.д.?')

    assert query == "Можно or перенести or отпуск or больничный or и or т.д"
    assert '"' not in query
    assert " -" not in query


def test_fulltext_query_keeps_codes_and_numbers() -> None:
    assert to_fulltext_query("Что в п. 3.2 про Старт-ИИ-1 и 1С:ERP?") == (
        "Что or в or п or 3.2 or про or Старт-ИИ-1 or и or 1С or ERP"
    )


def test_fulltext_query_removes_duplicates_case_insensitive() -> None:
    assert to_fulltext_query("Отпуск, отпуск и ОТПУСК") == "Отпуск or и"


def test_fulltext_query_empty() -> None:
    assert to_fulltext_query("?!") == ""


# --- expand_query ---------------------------------------------------------------------

GLOSSARY = {
    "ДМС": "добровольное медицинское страхование",
    "СЭД": "система электронного документооборота",
    "ОС": "операционная система",
    "1С": "программа 1С:Предприятие",
}


def test_expand_adds_expansion() -> None:
    assert expand_query("Как оформить ДМС?", GLOSSARY) == (
        "Как оформить ДМС? (ДМС — добровольное медицинское страхование)"
    )


def test_expand_multiple_terms_in_order_of_appearance() -> None:
    assert expand_query("Где в СЭД полис ДМС?", GLOSSARY) == (
        "Где в СЭД полис ДМС? (СЭД — система электронного документооборота; "
        "ДМС — добровольное медицинское страхование)"
    )


@pytest.mark.parametrize(
    "question", ["Кто оплачивает дмс?", "Что даёт по ДМСу?", "В СЭДе нет заявки"]
)
def test_expand_finds_case_and_endings(question: str) -> None:
    assert expand_query(question, GLOSSARY) != question


@pytest.mark.parametrize("question", ["Когда осень?", "ДМСКАЯ-чепуха", "аДМС"])
def test_expand_no_false_positives(question: str) -> None:
    assert expand_query(question, GLOSSARY) == question


def test_expand_skips_term_already_expanded() -> None:
    question = "ДМС (добровольное медицинское страхование) — как оформить?"

    assert expand_query(question, GLOSSARY) == question


def test_expand_empty_glossary_and_empty_values() -> None:
    assert expand_query("Как оформить ДМС?", {}) == "Как оформить ДМС?"
    assert (
        expand_query("Как оформить ДМС?", {"ДМС": "  ", " ": "x"})
        == "Как оформить ДМС?"
    )


def test_expand_term_with_digit() -> None:
    assert expand_query("Как зайти в 1С?", GLOSSARY).endswith(
        "(1С — программа 1С:Предприятие)"
    )


def test_expanded_query_feeds_fulltext() -> None:
    expanded = expand_query("Как оформить ДМС?", GLOSSARY)

    assert to_fulltext_query(expanded) == (
        "Как or оформить or ДМС or добровольное or медицинское or страхование"
    )
