"""Справочник оргструктуры (domain/directory.py): разбор и поиск по должности."""

# ruff: noqa: E501 — строки образца справочника длинные намеренно

from corp_ed.domain.directory import (
    Entry,
    Substitute,
    covers,
    format_entries,
    lookup,
    lookup_substitute,
    parse_directory,
    requested_fields,
    substitution_requested,
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
    assert requested_fields("адрес отдела персонала") == {"email"}
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


def test_lookup_role_with_nonbreaking_hyphen_and_latin_it() -> None:
    """Приёмка 4, T4-09: решение пишет «IT‑директор» (U+2011, латиница)."""
    entries = parse_directory(SAMPLE)
    for query in ("ИТ-директор", "ИТ\u2011директор", "IT\u2011директор", "IT-директор"):
        assert [e.role for e in lookup(entries, query)] == ["ИТ-директор"], query


# Замещение (фаза 0, 21.10): форма стенда — общее правило в «Замещении» и
# вышестоящие в «Подчинённости»; пар «X замещает Y» там нет.
STAND_TAIL = """\
— Коммерческий директор — Феоктист Ильич Самоваров, кабинет 305, samovarov@stk-test.example.
— Директор по персоналу — Злата Романовна Подберёзова, кабинет 210, podberezova@stk-test.example.
— Комплаенс — комплаенс-офицер Гордей Степанович Клюквин, кабинет 409.
Подчинённость
-------------
Финансовому директору подчиняются бухгалтерия, казначейство и ЦУП. Коммерческому директору подчиняются ОКП и региональные менеджеры. ИТ-директор и директор по персоналу подчиняются генеральному директору напрямую. Комплаенс-офицер независим и подотчётен генеральному директору.
Замещение
---------
На время отсутствия руководителя его обязанности исполняет заместитель, назначенный приказом. Если заместитель не назначен, вопросы решает вышестоящий руководитель.
"""
STAND = (
    SAMPLE.replace("Подчинённость\n", "").replace(
        "Финансовому директору подчиняются бухгалтерия, казначейство и ЦУП.\n", ""
    )
    + STAND_TAIL
)
RULE = "если заместитель не назначен, вопросы решает вышестоящий руководитель"


def _by_who(entries: list[Entry]) -> dict[str, Entry]:
    return {e.who(): e for e in entries}


def test_parse_superior_rule_from_subordination() -> None:
    entries = _by_who(parse_directory(STAND))
    expected = {
        "Бухгалтерия — главный бухгалтер": "Финансовый директор",
        "Центр учёта поставок (ЦУП) — руководитель": "Финансовый директор",
        "Отдел корпоративных продаж (ОКП) — руководитель": "Коммерческий директор",
        "ИТ-директор": "Генеральный директор",
        "Директор по персоналу": "Генеральный директор",
        # «подотчётен» — вышестоящий справа, не наоборот.
        "Комплаенс — комплаенс-офицер": "Генеральный директор",
        # «— Отдел персонала — подчиняется директору по персоналу, …».
        "Отдел персонала": "Директор по персоналу",
    }
    for who, deputy in expected.items():
        assert (entries[who].deputy, entries[who].deputy_note) == (deputy, RULE), who
    # Вышестоящего нет в тексте — замещающего нет; «казначейство»,
    # «региональные менеджеры» — не записи, пропущены.
    for who in ("Генеральный директор", "Финансовый директор", "Коммерческий директор"):
        assert entries[who].deputy == "", who
    assert entries["Охрана труда и пожарная безопасность — специалист"].deputy == ""


def test_no_rule_no_deputy() -> None:
    """Подчинённость без правила о вышестоящем — не замещение (SAMPLE)."""
    assert all(e.deputy == "" for e in parse_directory(SAMPLE))
    no_rule = STAND.replace(
        "Если заместитель не назначен, вопросы решает вышестоящий руководитель.", ""
    )
    assert all(e.deputy == "" for e in parse_directory(no_rule))


def test_parse_explicit_forms_and_skip_garbage() -> None:
    text = (
        STAND + "Главного бухгалтера при отсутствии замещает заместитель главного "
        "бухгалтера Нина Петровна Иванова, кабинет 208, доб. 1207.\n"
        "На время отсутствия коммерческого директора его обязанности исполняет "
        "руководитель ОКП.\n"
        # мусор: замещающий не назван / не найден / подразделения нет
        "При отсутствии комплаенс-офицера его замещает кто-нибудь из юристов.\n"
        "Отдел маркетинга подчиняется коммерческому директору.\n"
        "Вместо директора подписывает тот, кто рядом.\n"
    )
    entries = parse_directory(text)
    by = _by_who(entries)
    deputy = by["заместитель главного бухгалтера"]
    assert (deputy.name, deputy.room, deputy.phone) == (
        "Нина Петровна Иванова",
        "208",
        "1207",
    )
    # Явная пара важнее правила о вышестоящем.
    acc = by["Бухгалтерия — главный бухгалтер"]
    assert (acc.deputy, acc.deputy_note) == ("заместитель главного бухгалтера", "")
    assert (
        by["Коммерческий директор"].deputy
        == "Отдел корпоративных продаж (ОКП) — руководитель"
    )
    assert by["Коммерческий директор"].deputy_note == ""
    assert by["Комплаенс — комплаенс-офицер"].deputy == "Генеральный директор"
    assert all("маркетинг" not in e.who().casefold() for e in entries)
    # Обычный разбор строк не изменился.
    assert [e.name for e in entries[:3]] == [
        "Аристарх Леонидович Пряников",
        "Ярослава Игоревна Кукушкина",
        "Всеволод Петрович Ёлкин",
    ]


def test_lookup_and_format_unchanged_with_deputies() -> None:
    """BH-47: lookup и format_entries по-прежнему не показывают замещающего."""
    entries = parse_directory(STAND)
    assert format_entries(lookup(entries, "руководитель ЦУП")) == (
        "— Центр учёта поставок (ЦУП) — руководитель — Мирон Аркадьевич Черепашкин, "
        "кабинет 118."
    )


def test_substitution_requested() -> None:
    for text in (
        "руководитель ЦУП в отпуске и заместителя приказом не назначали",
        "замещающий главного бухгалтера",
        "к кому адресовать отчёт при отсутствии главного бухгалтера",
        "кто вместо финансового директора",
        "вышестоящий руководитель ОКП",
        "главбух на больничном",
    ):
        assert substitution_requested(text), text
    for text in (
        "Вернулся из командировки, сколько дней на отчёт?",
        "финансовый директор ФИО",
        "сколько дней отпуска положено",
    ):
        assert not substitution_requested(text), text


def test_lookup_substitute_pairs() -> None:
    entries = parse_directory(STAND)
    for role in (
        "руководитель ЦУП",  # T4-04: роль без слов о замещении
        "замещающий руководителя ЦУП",
        "вышестоящий руководитель ЦУП",
    ):
        pairs = lookup_substitute(entries, role)
        assert [(p.absent.name, p.deputy.name) for p in pairs] == [
            ("Мирон Аркадьевич Черепашкин", "Ярослава Игоревна Кукушкина")
        ], role
    for role in (
        "заместитель главного бухгалтера",  # T5-19
        "замещающий главного бухгалтера",
        "вышестоящий руководитель главного бухгалтера",
    ):
        pairs = lookup_substitute(entries, role)
        assert [p.deputy.role for p in pairs] == ["Финансовый директор"], role
    assert [p.deputy.name for p in lookup_substitute(entries, "руководитель ОКП")] == [
        "Феоктист Ильич Самоваров"
    ]
    # Замещающий неизвестен или роли нет — пусто, дальше обычный lookup.
    assert lookup_substitute(entries, "кто замещает финансового директора") == []
    assert lookup_substitute(entries, "финансовый директор") == []
    assert lookup_substitute(entries, "замещающий") == []
    assert lookup_substitute(parse_directory(SAMPLE), "руководитель ЦУП") == []


def test_lookup_substitute_finds_explicit_deputy_entry() -> None:
    text = STAND + (
        "Главного бухгалтера при отсутствии замещает заместитель главного "
        "бухгалтера Нина Петровна Иванова, кабинет 208.\n"
    )
    entries = parse_directory(text)
    for role in ("заместитель главного бухгалтера", "кто замещает главного бухгалтера"):
        pairs = lookup_substitute(entries, role)
        assert [(p.absent.role, p.deputy.name) for p in pairs] == [
            ("главный бухгалтер", "Нина Петровна Иванова")
        ], role


def test_format_and_covers_substitute() -> None:
    entries = parse_directory(STAND)
    pairs = lookup_substitute(entries, "руководитель ЦУП")
    assert format_entries(pairs) == (
        "— Центр учёта поставок (ЦУП) — руководитель — Мирон Аркадьевич Черепашкин, "
        "кабинет 118; при отсутствии: если заместитель не назначен, вопросы решает "
        "вышестоящий руководитель — Финансовый директор — Ярослава Игоревна "
        "Кукушкина, кабинет 402, доб. 1120, kukushkina@stk-test.example."
    )
    explicit = Substitute(
        absent=Entry(
            role="Главный бухгалтер", unit="", name="А Б В", email="a@x.example"
        ),
        deputy=Entry(role="заместитель главного бухгалтера", unit="", name="Г Д Е"),
    )
    assert explicit.text() == (
        "Главный бухгалтер — А Б В, a@x.example; при отсутствии замещает — "
        "заместитель главного бухгалтера — Г Д Е"
    )
    # Поля — замещающего: T5-19 просит почту, у главбуха её нет, у финдиректора есть.
    acc = lookup_substitute(entries, "заместитель главного бухгалтера")
    assert covers(acc, requested_fields("e-mail руководителя при отсутствии главбуха"))
    assert covers(acc, frozenset({"name", "room", "phone"}))
    # Почта есть у отсутствующего, у замещающего нет — не полное попадание.
    assert not covers([explicit], frozenset({"email"}))
    assert covers([explicit], frozenset({"name"}))
