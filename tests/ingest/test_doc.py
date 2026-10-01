"""Разбор .doc (Word 97–2003) → Markdown (Р-5, BH-35): текст, заголовки,
таблицы, поля, сноски, защита."""

import struct

import pytest

from corp_ed.ingest.doc import check_container, doc_to_markdown, read_document
from corp_ed.ingest.ooxml import OfficeFileError
from corp_ed.ingest.preprocess import preprocess
from tests.ingest.doc_samples import Para, Row, Style, cfb, doc, word_streams

HEADINGS = [
    Style(0, 0, "Normal"),
    Style(1, 1, "heading 1"),
    Style(2, 2, "heading 2"),
    Style(3, 62, "Title"),
    Style(4, 0xFFE, "Заголовок 3"),
    Style(5, 0xFFE, "Мой раздел", outline=1),
]


def _lines(data: bytes) -> list[str]:
    return [
        line for line in preprocess(doc_to_markdown(data)).splitlines() if line.strip()
    ]


def test_headings_from_styles_names_and_outline_levels() -> None:
    data = doc(
        [
            Para("Положение об оплате труда", istd=3),
            Para("1. Общие положения", istd=1),
            Para("Положение действует с 2026 года."),
            Para("1.1. Сроки", istd=2),
            Para("Подраздел по имени стиля", istd=4),
            Para("Свой стиль с уровнем структуры", istd=5),
            Para("Абзац с уровнем в самом абзаце", outline=0),
            Para("Обычный текст."),
        ],
        styles=HEADINGS,
    )
    assert read_document(data).headings == 6
    assert _lines(data) == [
        "# Положение об оплате труда",
        "# 1. Общие положения",
        "Положение действует с 2026 года.",
        "## 1.1. Сроки",
        "### Подраздел по имени стиля",
        "## Свой стиль с уровнем структуры",
        "# Абзац с уровнем в самом абзаце",
        "Обычный текст.",
    ]


def test_bold_paragraphs_become_headings_when_there_are_no_styles() -> None:
    data = doc(
        [
            Para("Перед поездкой", bold=True),
            Para("Заявку подайте за 5 рабочих дней."),
            Para("После возвращения", bold=True),
            Para("Отчёт — в течение 3 рабочих дней."),
        ]
    )
    assert "**Перед поездкой**" in doc_to_markdown(data)
    # Уровень у полужирной строки без номера ставит preprocess (правило 8).
    assert _lines(data) == [
        "## Перед поездкой",
        "Заявку подайте за 5 рабочих дней.",
        "## После возвращения",
        "Отчёт — в течение 3 рабочих дней.",
    ]


def _table_paragraphs() -> list[Para]:
    # Шапка в две строки: «Отдел» объединён по вертикали, «Численность» —
    # одна широкая ячейка над «2025» и «2026»; строка-группа во всю ширину.
    return [
        Para("Отдел", cell_end=True),
        Para("Численность", cell_end=True),
        Para("", ttp=True, row=Row([0, 2000, 6000], [0x0060, 0])),
        Para("", cell_end=True),
        Para("2025", cell_end=True),
        Para("2026", cell_end=True),
        Para("", ttp=True, row=Row([0, 2000, 4000, 6000], [0x0020, 0, 0])),
        Para("Москва", cell_end=True),
        Para("", ttp=True, row=Row([0, 6000])),
        Para("Продажи", cell_end=True),
        Para("34", cell_end=True),
        Para("41", cell_end=True),
        Para("", ttp=True, row=Row([0, 2000, 4000, 6000])),
    ]


def test_table_with_spans_vertical_merge_and_group_row() -> None:
    data = doc(
        [Para("Численность", istd=1), *_table_paragraphs(), Para("После таблицы.")],
        styles=HEADINGS,
    )
    assert read_document(data).tables == 1
    assert _lines(data) == [
        "# Численность",
        "## Москва",
        "Отдел: Продажи; Численность — 2025: 34; Численность — 2026: 41",
        "После таблицы.",
    ]


def test_wide_table_row_end_properties_in_data_stream() -> None:
    # Word 7-столбцовую таблицу хранит так: конец строки — sprmPHugePapx.
    header = [Para(name, cell_end=True) for name in ("День", "Дата", "Время")]
    row = [Para(value, cell_end=True) for value in ("1", "05.10.2026", "10:00")]
    bounds = Row([0, 1000, 2000, 3000])
    data = doc(
        [
            *header,
            Para("", ttp=True, row=bounds, huge=True),
            *row,
            Para("", ttp=True, row=bounds, huge=True),
        ]
    )
    assert _lines(data) == ["День: 1; Дата: 05.10.2026; Время: 10:00"]


def test_old_style_horizontal_merge_flag() -> None:
    data = doc(
        [
            Para("Вид", cell_end=True),
            Para("Сумма", cell_end=True),
            Para("", cell_end=True),
            Para("", ttp=True, row=Row([0, 1000, 2000, 3000], [0, 0x0001, 0x0002])),
            Para("Годовая", cell_end=True),
            Para("1 оклад", cell_end=True),
            Para("в марте", cell_end=True),
            Para("", ttp=True, row=Row([0, 1000, 2000, 3000])),
        ]
    )
    assert _lines(data) == ["Вид: Годовая; Сумма: 1 оклад; Сумма: в марте"]


def test_table_without_row_marks_falls_back_to_empty_cell_after_cell() -> None:
    paragraphs = [
        Para("Имя", cell_end=True, raw_props=False),
        Para("Телефон", cell_end=True, raw_props=False),
        Para("", cell_end=True, raw_props=False),
        Para("Анна", cell_end=True, raw_props=False),
        Para("1021", cell_end=True, raw_props=False),
        Para("", cell_end=True, raw_props=False),
    ]
    assert _lines(doc(paragraphs)) == ["Имя: Анна; Телефон: 1021"]


def test_multi_paragraph_cells_and_nested_text_join() -> None:
    data = doc(
        [
            Para("Шаг", cell_end=True),
            Para("Что сделать", cell_end=True),
            Para("", ttp=True, row=Row([0, 1000, 3000])),
            Para("1", cell_end=True),
            Para("Позвонить в ИТ", in_table=True),
            Para("и сменить пароль", cell_end=True),
            Para("", ttp=True, row=Row([0, 1000, 3000])),
        ]
    )
    assert _lines(data) == ["Шаг: 1; Что сделать: Позвонить в ИТ и сменить пароль"]


def test_fields_keep_result_and_drop_code_and_table_of_contents() -> None:
    data = doc(
        [
            Para('\x13 TOC \\o "1-3" \x14Содержание раздела 1\t3\x15'),
            Para('Подробнее на \x13 HYPERLINK "https://portal" \x14портале\x15.'),
            Para("Раздел \x13 SEQ part \x147\x15 положения"),
            Para("См. \x13 HYPERLINK \x13 REF x \x14лишнее\x15 \x14раздел 2\x15"),
        ]
    )
    # Вложенное поле в коде внешнего выбрасывается вместе с кодом.
    assert _lines(data) == [
        "Подробнее на портале.",
        "Раздел 7 положения",
        "См. раздел 2",
    ]


def test_compressed_pieces_special_characters_and_surrogates() -> None:
    data = doc(
        [
            Para("Plain ASCII piece", compressed=True),
            Para("Строка\x0bвторая строка"),
            Para("Нерaзрывный\xa0пробел и не\x1eразрывный дефис"),
            Para("Эмодзи 🙂 цел"),
        ]
    )
    assert _lines(data) == [
        "Plain ASCII piece",
        "Строка",
        "вторая строка",
        "Нерaзрывный пробел и не-разрывный дефис",
        "Эмодзи 🙂 цел",
    ]


def test_deleted_revision_text_is_dropped() -> None:
    data = doc(
        [
            Para("Срок — 10 рабочих дней.", deleted=True),
            Para("Срок — 5 рабочих дней."),
        ]
    )
    assert _lines(data) == ["Срок — 5 рабочих дней."]


def test_footnotes_are_appended() -> None:
    document = read_document(
        doc(
            [Para("Надбавка за язык — 10 % оклада.")],
            footnotes=["Нужен сертификат B2."],
        )
    )
    assert document.footnotes == 1
    assert document.blocks[-1] == "Сноски:\nНужен сертификат B2."


def test_container_checks() -> None:
    with pytest.raises(OfficeFileError) as error:
        check_container("{\\rtf1\\ansi текст}".encode("cp1251"))
    assert error.value.code == "format_mismatch"

    with pytest.raises(OfficeFileError) as error:
        check_container(cfb({"Workbook": b"\0" * 5000}))
    assert error.value.code == "format_mismatch"

    with pytest.raises(OfficeFileError) as error:
        check_container(
            cfb({"EncryptedPackage": b"\0" * 100, "EncryptionInfo": b"\0" * 10})
        )
    assert error.value.code == "encrypted"

    for flags, nfib, code in (
        (0x1300, 0x00C1, "encrypted"),
        (0x1200, 0x0065, "unsupported_format"),
    ):
        with pytest.raises(OfficeFileError) as error:
            check_container(doc([Para("x")], flags=flags, nfib=nfib))
        assert error.value.code == code


def test_broken_files_are_corrupted() -> None:
    good = doc([Para("Текст")])
    with pytest.raises(OfficeFileError) as error:
        read_document(good[:1500])
    assert error.value.code == "corrupted"

    with pytest.raises(OfficeFileError) as error:
        read_document(cfb(word_streams([Para("Текст")]), fat_loop=True))
    assert error.value.code == "corrupted"

    streams = word_streams([Para("Текст")])
    word = bytearray(streams["WordDocument"])
    struct.pack_into("<II", word, 0x9A + 8 * 33, 0, 0)  # нет таблицы кусков
    with pytest.raises(OfficeFileError) as error:
        read_document(cfb({**streams, "WordDocument": bytes(word)}))
    assert error.value.code == "corrupted"


def test_empty_document() -> None:
    assert doc_to_markdown(doc([Para("")])) == ""
