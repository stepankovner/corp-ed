"""Решение до ответа (decision-v1): модель решения, маршрут, оговорка."""

import json

import pytest

from corp_ed.domain.decision import (
    Decision,
    Route,
    mismatch_note,
    parse_decision,
    route,
    search_query,
)


def make(**fields: str) -> Decision:
    base = {
        "kind": "question",
        "subject": "—",
        "excerpts_subject": "—",
        "match": "n/a",
        "sufficiency": "full",
        "missing": "—",
        "search_query": "—",
    }
    return Decision.model_validate({**base, **fields})


def test_parse_valid_and_optional_fields() -> None:
    text = json.dumps(
        {
            "reasoning": "Спрашивают имя, в выдержке [1] — только должность.",
            "kind": "question",
            "subject": "—",
            "excerpts_subject": "Регламент закупок",
            "match": "n/a",
            "sufficiency": "partial",
            "missing": "ФИО финансового директора",
            "search_query": "финансовый директор ФИО",
        },
        ensure_ascii=False,
    )
    decision = parse_decision(text)
    assert decision is not None
    assert decision.sufficiency == "partial"
    assert decision.answer == ""


@pytest.mark.parametrize(
    "text",
    [
        "не JSON",
        '{"kind": "question"}',  # не хватает полей
        json.dumps({**make().model_dump(), "match": "maybe"}),  # значение вне перечня
        json.dumps({**make().model_dump(), "extra": 1}),  # лишнее поле
    ],
)
def test_parse_rejects_schema_violations(text: str) -> None:
    assert parse_decision(text) is None


def test_route_order() -> None:
    assert route(make(kind="small_talk", sufficiency="none"), can_search=True) == (
        Route.SMALL_TALK
    )
    # Про другое — «ответа нет», даже если модель сочла выдержки достаточными.
    assert route(make(match="other", sufficiency="full"), can_search=True) == (
        Route.OTHER_SUBJECT
    )
    assert route(make(sufficiency="none"), can_search=True) == Route.NOT_FOUND
    partial = make(sufficiency="partial", search_query="финансовый директор ФИО")
    assert route(partial, can_search=True) == Route.SEARCH
    assert route(partial, can_search=False) == Route.ANSWER
    assert route(make(match="other_edition"), can_search=True) == Route.ANSWER


def test_partial_without_query_answers() -> None:
    assert route(make(sufficiency="partial"), can_search=True) == Route.ANSWER


def test_search_query_cleanup() -> None:
    assert search_query(make(search_query="—")) is None
    assert search_query(make(search_query=" «финансовый директор ФИО». ")) == (
        "финансовый директор ФИО"
    )
    long = " ".join(f"слово{i}" for i in range(20))
    query = search_query(make(search_query=long))
    assert query is not None and len(query.split()) == 12


def test_mismatch_note() -> None:
    decision = make(
        subject="ГПБ Тех 2025",
        excerpts_subject="«Акселератор ГПБ Тех 2026»",
        match="other_edition",
    )
    assert mismatch_note(decision) == (
        "В документах — «Акселератор ГПБ Тех 2026», а не «ГПБ Тех 2025»."
    )
    assert mismatch_note(make(subject="Старт-ИИ-2", excerpts_subject="—")) == ""
    assert mismatch_note(make(subject="УМНИК", excerpts_subject="умник")) == ""
