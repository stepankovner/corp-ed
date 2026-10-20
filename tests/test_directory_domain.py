"""Справочник оргструктуры (domain/directory.py): разбор и поиск по должности."""

# ruff: noqa: E501 — строки образца справочника длинные намеренно

from corp_ed.domain.directory import (
    Entry,
    covers,
    format_entries,
    lookup,
    parse_directory,
    requested_fields,
)

SAMPLE = """\
ОРГАНИЗАЦИОННАЯ СТРУКТУРА
Руководство
— Генеральный директор — Аристарх Леонидович Пряников, кабинет 401, priyanikov@stk-test.example.
— Финансовый директор — Ярослава Игоревна Кукушкина, кабинет 402, доб. 1120, kukushkina@stk-test.example.
— ИТ-директор — Всеволод Петрович Ёлкин, кабинет 314-Б, elkin@stk-test.example.
Подразделения
— Бухгалтерия — главный бухгалтер Олимпиада Сергеевна Брусникина, кабинет 207.
— Центр учёта поставок (ЦУП) — руководитель Мирон Аркадьевич Черепашкин, кабинет 118.
— Отдел корпоративных продаж (ОКП) — руководитель Таисия Олеговна Пастилова, кабинет 301.
— Отдел персонала — подчиняется директору по персоналу, кабинет 210.
— Охрана труда и пожарная безопасность — специалист Ефросинья Павловна Грибова, кабинет 108.
Подчинённость
Финансовому директору подчиняются бухгалтерия, казначейство и ЦУП.
"""


def test_parse() -> None:
    entries = parse_directory(SAMPLE)
    by_name = {e.name: e for e in entries}
    fin = by_name["Ярослава Игоревна Кукушкина"]
    assert (fin.role, fin.unit, fin.room, fin.phone, fin.email) == (
        "Финансовый директор",
        "",
        "402",
        "1120",
        "kukushkina@stk-test.example",
    )
    assert by_name["Всеволод Петрович Ёлкин"].room == "314-Б"
    acc = by_name["Олимпиада Сергеевна Брусникина"]
    assert (acc.role, acc.unit, acc.room) == ("главный бухгалтер", "Бухгалтерия", "207")
    okp = by_name["Таисия Олеговна Пастилова"]
    assert okp.unit == "Отдел корпоративных продаж (ОКП)" and okp.role == "руководитель"
    hr = next(e for e in entries if e.unit == "" and e.role == "Отдел персонала")
    assert hr.name == "" and hr.room == "210"
    # Строки без тире-разделителя (заголовки, абзацы) не записи.
    assert all(e.role != "Подчинённость" for e in entries)


def test_lookup_by_role_and_unit() -> None:
    entries = parse_directory(SAMPLE)
    assert [e.name for e in lookup(entries, "финансовый директор ФИО")] == [
        "Ярослава Игоревна Кукушкина"
    ]
    assert [
        e.name for e in lookup(entries, "Финансового директора e-mail и кабинет")
    ] == ["Ярослава Игоревна Кукушкина"]
    assert [e.name for e in lookup(entries, "руководитель ОКП кабинет")] == [
        "Таисия Олеговна Пастилова"
    ]
    assert [e.name for e in lookup(entries, "главный бухгалтер фамилия")] == [
        "Олимпиада Сергеевна Брусникина"
    ]
    assert [e.name for e in lookup(entries, "специалист по охране труда")] == [
        "Ефросинья Павловна Грибова"
    ]
    assert lookup(entries, "комплаенс-офицер ФИО") == []
    assert lookup(entries, "директор") != []  # неоднозначно — несколько записей
    assert lookup(entries, "ФИО") == []


def test_format() -> None:
    entries = lookup(parse_directory(SAMPLE), "руководитель ЦУП")
    assert format_entries(entries) == (
        "— Центр учёта поставок (ЦУП) — руководитель — Мирон Аркадьевич Черепашкин, "
        "кабинет 118."
    )
    assert Entry(role="Отдел персонала", unit="", name="", room="210").text() == (
        "Отдел персонала — кабинет 210"
    )


def test_requested_fields() -> None:
    assert requested_fields("ФИО и кабинет") == {"name", "room"}
    assert requested_fields("конкретный email\u2011адрес отдела персонала") == {"email"}
    assert requested_fields("добавочный номер финансового директора") == {"phone"}
    assert requested_fields("номер кабинета") == {"room"}
    assert requested_fields("—") == frozenset()


def test_covers_needs_all_requested_fields() -> None:
    entries = parse_directory(SAMPLE)
    hr = lookup(entries, "отдел персонала")
    assert hr and not covers(hr, frozenset({"email"}))
    assert covers(hr, frozenset({"room"}))
    fin = lookup(entries, "финансовый директор")
    assert covers(fin, frozenset({"name", "phone", "email"}))
    assert covers(fin, frozenset())
    assert not covers([], frozenset())
