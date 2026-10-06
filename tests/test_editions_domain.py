"""Подсказка «новая редакция заменяет прежнюю» (BH-41, решение Артёма 06.10)."""

from corp_ed.domain.editions import (
    cancels_previous_edition,
    edition_key,
    edition_year,
    superseded_candidates,
)

STAND = {
    1: "komandirovki 2025",
    2: "hr otpuska i otguly",
    3: "orgstruktura",
    4: "svod reglamentov",
}
STAND_2026_TEXT = (
    "Утверждено приказом № 7-К от 15.01.2026. Настоящая редакция вступает в "
    "силу с 01.02.2026; предыдущая редакция (2025 года) утрачивает силу с той "
    "же даты."
)


def test_key_drops_year_and_edition_words() -> None:
    assert edition_key("Положение о командировках (редакция 2026 года)") == (
        "положение о командировках"
    )
    assert edition_key("komandirovki_2025") == "komandirovki"
    assert edition_year("Положение (ред. 2024), версия от 2025 г.") == 2025
    assert edition_year("orgstruktura") is None


def test_stand_new_edition_points_to_the_old_one() -> None:
    # Корпус стенда: положение 2026 года заменяет 2025-го, остальное — нет.
    assert superseded_candidates("komandirovki 2026", STAND_2026_TEXT, STAND) == [1]
    assert superseded_candidates("komandirovki 2026", "", STAND) == [1]
    assert superseded_candidates("zakupki reglament", "", STAND) == []


def test_programs_and_queues_are_not_editions() -> None:
    # Подмена программы — главный риск: «Старт-ИИ-1 (очередь 2)» и
    # «Старт-ИИ-2» — разные документы, номер — не год.
    existing = {"a": "Положение Старт-ИИ-1 (очередь 2)_на сайт", "b": "UMNIK-2025"}

    assert superseded_candidates("Положение Старт-ИИ-2_на сайт", "", existing) == []
    assert superseded_candidates("UMNIK-2026", "", existing) == ["b"]


def test_older_or_same_title_without_cancel_is_not_a_candidate() -> None:
    existing = {"old": "Регламент закупок (редакция 2026 года)"}

    # Загружают более старую редакцию — она ничего не заменяет.
    assert (
        superseded_candidates("Регламент закупок (редакция 2024 года)", "", existing)
        == []
    )
    # То же название без года — только если в тексте сказано об отмене.
    plain = {"old": "Регламент закупок"}
    assert superseded_candidates("Регламент закупок", "", plain) == []
    text = "Прежняя редакция регламента отменяется с 01.03.2026."
    assert cancels_previous_edition(text)
    assert superseded_candidates("Регламент закупок", text, plain) == ["old"]
    assert superseded_candidates("2026", "", {"x": "2025"}) == []
