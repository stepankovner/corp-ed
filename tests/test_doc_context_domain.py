"""Контекст документа в чанке: карточка, строка контекста, статус редакции."""

from corp_ed.domain.doc_context import (
    CONTEXT_PREFIX,
    DocContext,
    context_phrase,
    document_head,
    edition_statuses,
    insert_context,
    parse_doc_context,
)


def card(**fields: str) -> DocContext:
    base = {
        "title": "Правила отбора акселератора ГПБ Тех 2026",
        "organization": "Газпромбанк",
        "program": "Акселератор ГПБ Тех",
        "track": "Финтех. Стартапы",
        "year": "2026",
        "edition": "—",
    }
    return DocContext(**{**base, **fields})


def test_phrase_names_track_and_skips_dash_and_repeats() -> None:
    phrase = context_phrase(card())
    assert phrase.startswith(CONTEXT_PREFIX)
    assert "«Правила отбора акселератора ГПБ Тех 2026»" in phrase
    assert "трек или направление: Финтех. Стартапы" in phrase
    assert "организация: Газпромбанк" in phrase
    # Год уже в названии, редакции нет — не повторяем и не пишем «—».
    assert "год:" not in phrase
    assert "редакция" not in phrase
    assert "—" not in phrase
    assert phrase.endswith(".")
    assert "\n" not in phrase


def test_phrase_status_only_when_given() -> None:
    assert "действующая" not in context_phrase(card())
    assert context_phrase(card(), "current").endswith("действующая редакция.")
    assert "заменена" in context_phrase(card(), "superseded")


def test_phrase_trims_long_fields_and_empty_card() -> None:
    phrase = context_phrase(card(program="слово " * 100))
    assert "…" in phrase
    empty = DocContext(
        title="—", organization="", program="нет", track="—", year="", edition=""
    )
    assert context_phrase(empty) == ""
    assert context_phrase(empty, "superseded") == (
        f"{CONTEXT_PREFIX}прежняя редакция, заменена новой."
    )


def test_parse_doc_context_rejects_bad_schema() -> None:
    good = card().model_dump_json()
    assert parse_doc_context(good) == card()
    assert parse_doc_context('{"title": "x"}') is None
    assert parse_doc_context("не JSON") is None
    assert parse_doc_context(good[:-1] + ', "extra": "1"}') is None


def test_document_head_cuts_on_line_boundary() -> None:
    text = "\n".join(f"Строка номер {i} с текстом" for i in range(1000))
    head = document_head(text, max_tokens=100)
    assert len(head) <= 300
    assert text.startswith(head)
    assert head.endswith("с текстом")
    assert document_head("коротко", max_tokens=100) == "коротко"


def test_edition_statuses_marks_old_edition() -> None:
    statuses = edition_statuses(
        {
            "k25": ("komandirovki 2025", "Положение 2025"),
            "k26": ("komandirovki 2026", "Положение 2026"),
            "hr": ("hr otpuska i otguly", "Положение об отпусках"),
            "s1": ("Положение Старт-ИИ-1 (очередь 2)", "…"),
            "w1": ("Победители_Старт-ИИ-1 (очередь 2)", "…"),
        }
    )
    assert statuses == {"k26": "current", "k25": "superseded"}


def test_edition_statuses_three_editions() -> None:
    statuses = edition_statuses(
        {
            "a": ("Регламент 2026", ""),
            "b": ("Регламент 2024", ""),
            "c": ("Регламент 2025", ""),
        }
    )
    assert statuses == {"a": "current", "b": "superseded", "c": "superseded"}


def test_insert_context_second_line_after_crumbs() -> None:
    crumbs = "Правила > 5. Требования"
    text = f"{crumbs}\nУчастник — юрлицо РФ."
    phrase = f"{CONTEXT_PREFIX}«Правила»."
    assert insert_context(text, crumbs, phrase) == (
        f"{crumbs}\n{phrase}\nУчастник — юрлицо РФ."
    )
    assert insert_context(text, crumbs, "") == text
    # Без крошек (v1, E2) — первой строкой.
    assert insert_context("Текст", "", phrase) == f"{phrase}\nТекст"
    assert insert_context("Текст", crumbs, phrase) == f"{phrase}\nТекст"
    assert insert_context(crumbs, crumbs, phrase) == f"{crumbs}\n{phrase}"
