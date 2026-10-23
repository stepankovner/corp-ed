"""Решение до ответа (decision-v1): модель решения, маршрут, оговорка."""

import json

import pytest

from corp_ed.domain.decision import (
    Decision,
    Route,
    edition_note,
    is_refusal,
    mismatch_note,
    note_end,
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
    other = make(match="other", subject="Старт-ИИ-2", sufficiency="full")
    assert route(other, can_search=True) == (Route.OTHER_SUBJECT)
    assert route(make(sufficiency="none"), can_search=True) == Route.NOT_FOUND
    partial = make(sufficiency="partial", search_query="финансовый директор ФИО")
    assert route(partial, can_search=True) == Route.SEARCH
    assert route(partial, can_search=False) == Route.ANSWER
    assert route(make(match="other_edition"), can_search=True) == Route.ANSWER


def test_other_edition_answers_even_when_none() -> None:
    # Приёмка 3: «ничего» при другой редакции — оценка по спрошенной
    # редакции; ответ по найденной откажет сам, если по теме ничего нет.
    edition = make(match="other_edition", subject="П-ОТП-07, редакция 3")
    none = edition.model_copy(update={"sufficiency": "none"})
    assert route(none, can_search=True) == Route.ANSWER
    partial = edition.model_copy(
        update={"sufficiency": "partial", "search_query": "П-ОТП-07 отгулы"}
    )
    assert route(partial, can_search=True) == Route.SEARCH
    assert route(make(match="same", sufficiency="none"), can_search=True) == (
        Route.NOT_FOUND
    )


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


def test_edition_note_and_note_end() -> None:
    named = make(subject="УМНИК-2025", excerpts_subject="УМНИК-2026")
    assert edition_note(named) == (
        "В документах — «УМНИК-2026», а не «УМНИК-2025»; в запрошенной "
        "редакции условия могли отличаться."
    )
    assert edition_note(make()).startswith("В документах — другой год или редакция")
    text = "В документах — «Sprint 2.0 (24.04.2026)», а не «Спринт 3.0». Ответ."
    assert text[note_end(text) - 1 :].startswith("». Ответ")
    assert note_end("В документах — редакция 4, а не 3") is None


def test_other_needs_named_subject() -> None:
    # «Про другое» без названного в вопросе — не с чем сравнивать.
    assert route(make(match="other", subject="—"), can_search=True) == Route.ANSWER
    assert route(
        make(match="other", subject="—", sufficiency="none"), can_search=True
    ) == (Route.NOT_FOUND)


def test_directory_on_refusal_route() -> None:
    # Фаза 2 (план §10.4): отказ при названной роли — сначала справочник,
    # только с флагом и подключённым справочником.
    none_role = make(sufficiency="none", lookup_role="руководитель ОКП")
    other_role = make(
        match="other", subject="Старт-ИИ-2", lookup_role="руководитель ОКП"
    )
    for refusal, before in (
        (none_role, Route.NOT_FOUND),
        (other_role, Route.OTHER_SUBJECT),
    ):
        assert is_refusal(refusal)
        assert route(refusal, can_search=True, directory=True) == before
        assert route(refusal, can_search=True, directory_on_refusal=True) == before
        assert (
            route(refusal, can_search=True, directory=True, directory_on_refusal=True)
            == Route.DIRECTORY
        )
    # Без роли, при другой редакции и на реплике — как было.
    flags = {"can_search": True, "directory": True, "directory_on_refusal": True}
    assert route(make(sufficiency="none"), **flags) == Route.NOT_FOUND
    assert route(make(sufficiency="none", lookup_role="—"), **flags) == Route.NOT_FOUND
    edition = make(match="other_edition", sufficiency="none", lookup_role="директор")
    assert not is_refusal(edition) and route(edition, **flags) == Route.ANSWER
    talk = make(kind="small_talk", sufficiency="none", lookup_role="директор")
    assert not is_refusal(talk) and route(talk, **flags) == Route.SMALL_TALK
    # «Про другое» без названного в вопросе — не отказ.
    assert not is_refusal(make(match="other", sufficiency="full"))
