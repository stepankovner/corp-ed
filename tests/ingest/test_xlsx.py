"""Разбор .xlsx → Markdown (Р-5, BH-33): таблицы, объединения, форматы, защита."""

import io
import pyexpat
import zipfile

import pytest

from corp_ed.ingest import xlsx as xlsx_module
from corp_ed.ingest.preprocess import preprocess
from corp_ed.ingest.xlsx import (
    XlsxError,
    check_container,
    format_number,
    read_workbook,
    xlsx_to_markdown,
)
from tests.ingest.xlsx_samples import (
    Bool,
    Err,
    Formula,
    Inline,
    Num,
    SheetSpec,
    xlsx,
)


def _lines(markdown: str) -> list[str]:
    return [line for line in preprocess(markdown).splitlines() if line.strip()]


def test_table_rows_become_key_value_lines_after_preprocess() -> None:
    data = xlsx(
        [
            SheetSpec(
                "Лист1",
                {
                    "A1": "Страна",
                    "B1": "Суточные, ₽",
                    "A2": "Франция",
                    "B2": 2500,
                    "A3": "Казахстан",
                    "B3": 1800,
                },
            )
        ]
    )
    markdown = xlsx_to_markdown(data)

    assert "| Страна | Суточные, ₽ |" in markdown
    # Стандартное имя листа в крошки не идёт.
    assert "Лист1" not in markdown
    assert _lines(markdown) == [
        "Страна: Франция; Суточные, ₽: 2500",
        "Страна: Казахстан; Суточные, ₽: 1800",
    ]


def test_sheet_names_become_headings_and_hidden_sheets_are_skipped() -> None:
    data = xlsx(
        [
            SheetSpec(
                "Суточные", {"A1": "Страна", "B1": "Сумма", "A2": "Франция", "B2": 1}
            ),
            SheetSpec("Справочник", {"A1": "скрыто", "B1": "x"}, state="hidden"),
            SheetSpec(
                "Проживание", {"A1": "Город", "B1": "Лимит", "A2": "Москва", "B2": 2}
            ),
        ]
    )
    workbook = read_workbook(data)
    markdown = xlsx_to_markdown(data)

    assert [sheet.name for sheet in workbook.sheets] == ["Суточные", "Проживание"]
    assert workbook.hidden_sheets == ["Справочник"]
    assert markdown.index("# Суточные") < markdown.index("# Проживание")
    assert "скрыто" not in markdown


def test_short_text_above_table_becomes_its_heading() -> None:
    data = xlsx(
        [
            SheetSpec(
                "Нормы",
                {
                    "A1": "ООО «Пример»",
                    "A2": "Нормы суточных на 2026 год",
                    "A4": "Страна",
                    "B4": "Сумма",
                    "A5": "Франция",
                    "B5": 2500,
                    "A7": "* Суммы без НДФЛ",
                },
            )
        ]
    )
    lines = _lines(xlsx_to_markdown(data))

    assert lines == [
        "# Нормы",
        "ООО «Пример»",
        "## Нормы суточных на 2026 год",
        "Страна: Франция; Сумма: 2500",
        "* Суммы без НДФЛ",
    ]


def test_merged_cells_are_copied_and_two_row_header_is_joined() -> None:
    data = xlsx(
        [
            SheetSpec(
                "Суточные",
                {
                    "A1": "Регион",
                    "B1": "Страна",
                    "C1": "Суточные, ₽",
                    "C2": "до 10 дней",
                    "D2": "свыше 10 дней",
                    "A3": "Европа",
                    "B3": "Франция",
                    "C3": 2500,
                    "D3": 2000,
                    "B4": "Германия",
                    "C4": 2400,
                    "D4": 1900,
                },
                merges=["A1:A2", "B1:B2", "C1:D1", "A3:A4"],
            )
        ]
    )
    lines = _lines(xlsx_to_markdown(data))

    assert lines[1:] == [
        "Регион: Европа; Страна: Франция; Суточные, ₽ — до 10 дней: 2500; "
        "Суточные, ₽ — свыше 10 дней: 2000",
        "Регион: Европа; Страна: Германия; Суточные, ₽ — до 10 дней: 2400; "
        "Суточные, ₽ — свыше 10 дней: 1900",
    ]


def test_single_value_row_inside_table_is_a_group_heading() -> None:
    data = xlsx(
        [
            SheetSpec(
                "Контакты",
                {
                    "A1": "Кто",
                    "B1": "Телефон",
                    "A2": "Отдел кадров",
                    "A3": "Иванова",
                    "B3": "101",
                    "A4": "Бухгалтерия",
                    "A5": "Петров",
                    "B5": "202",
                },
                merges=["A2:B2", "A4:B4"],
            )
        ]
    )
    markdown = xlsx_to_markdown(data)

    assert _lines(markdown) == [
        "# Контакты",
        "## Отдел кадров",
        "Кто: Иванова; Телефон: 101",
        "## Бухгалтерия",
        "Кто: Петров; Телефон: 202",
    ]
    # Шапка повторяется после каждой группы, а не печатается пустой.
    assert markdown.count("| Кто | Телефон |") == 2


def test_row_with_empty_cells_is_data_not_a_group() -> None:
    data = xlsx(
        [
            SheetSpec(
                "Лист1",
                {
                    "A1": "Кто",
                    "B1": "Телефон",
                    "A2": "Петров",
                    "A3": "Иванова",
                    "B3": "101",
                },
            )
        ]
    )
    assert _lines(xlsx_to_markdown(data)) == [
        "Кто: Петров",
        "Кто: Иванова; Телефон: 101",
    ]


def test_first_column_value_in_wide_table_is_a_group() -> None:
    data = xlsx(
        [
            SheetSpec(
                "Лист1",
                {
                    "A1": "Город",
                    "B1": "Специалисты",
                    "C1": "Руководители",
                    "A2": "Россия",
                    "A3": "Казань",
                    "B3": 3500,
                    "C3": 5000,
                },
            )
        ]
    )
    assert _lines(xlsx_to_markdown(data)) == [
        "# Россия",
        "Город: Казань; Специалисты: 3500; Руководители: 5000",
    ]


def test_numeric_first_row_is_not_a_header() -> None:
    data = xlsx([SheetSpec("Лист1", {"A1": 1, "B1": 2, "A2": 3, "B2": 4})])
    assert _lines(xlsx_to_markdown(data)) == ["1; 2", "3; 4"]


def test_values_are_shown_as_excel_shows_them() -> None:
    cells = {
        "A1": "Что",
        "B1": "Значение",
        "A2": "процент",
        "B2": Num(0.155, "0.0%"),
        "A3": "дробь",
        "B3": Num(1.5, "0.00"),
        "A4": "дата",
        "B4": Num(46296, "dd/mm/yyyy"),
        "A5": "время",
        "B5": Num(0.375, "h:mm"),
        "A6": "дата и время",
        "B6": Num(46296.5, "dd.mm.yyyy hh:mm"),
        "A7": "рубли",
        "B7": Num(6500, '#,##0 "₽"'),
        "A8": "флаг",
        "B8": Bool(True),
        "A9": "ошибка",
        "B9": Err(),
        "A10": "в ячейке",
        "B10": Inline("строка"),
        "A11": "без формата",
        "B11": 0.1 + 0.2,
    }
    lines = _lines(xlsx_to_markdown(xlsx([SheetSpec("Лист1", cells)])))

    assert lines == [
        "Что: процент; Значение: 15,5%",
        "Что: дробь; Значение: 1,50",
        "Что: дата; Значение: 01.10.2026",
        "Что: время; Значение: 09:00",
        "Что: дата и время; Значение: 01.10.2026 12:00",
        "Что: рубли; Значение: 6500 ₽",
        "Что: флаг; Значение: да",
        "Что: ошибка",
        "Что: в ячейке; Значение: строка",
        "Что: без формата; Значение: 0,3",
    ]


def test_date_1904_system() -> None:
    assert format_number(0, "dd.mm.yyyy", date1904=True) == "01.01.1904"
    assert format_number(46296, "dd.mm.yyyy") == "01.10.2026"
    # Дата вне диапазона Excel — просто число.
    assert format_number(-1, "dd.mm.yyyy") == "-1"
    assert format_number(-0.0001, "0.00") == "0,00"
    assert format_number(1234.5, "#,##0.00\\ [$€-407]") == "1234,50 €"
    assert format_number(5, "[$-419]0") == "5"


def test_formula_uses_cached_value_and_counts_missing_ones() -> None:
    data = xlsx(
        [
            SheetSpec(
                "Лист1",
                {
                    "A1": "Итого",
                    "B1": Formula(cached=1500),
                    "A2": "Текст",
                    "B2": Formula(cached="готово"),
                    "A3": "Пусто",
                    "B3": Formula(cached=None),
                },
            )
        ]
    )
    workbook = read_workbook(data)

    assert workbook.formulas_without_value == 1
    assert workbook.sheets[0].cells[(1, 2)] == "1500"
    assert workbook.sheets[0].cells[(2, 2)] == "готово"
    assert (3, 2) not in workbook.sheets[0].cells


def test_cell_text_with_pipes_and_line_breaks_keeps_table_intact() -> None:
    data = xlsx(
        [
            SheetSpec(
                "Лист1",
                {
                    "A1": "Кто",
                    "B1": "Как",
                    "A2": "Охрана",
                    "B2": "звонить | писать\nпо будням",
                },
            )
        ]
    )
    assert _lines(xlsx_to_markdown(data)) == [
        "Кто: Охрана; Как: звонить | писать по будням"
    ]


def test_shared_strings_rich_text_escapes_and_phonetics() -> None:
    shared = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<si><r><t>Глав</t></r><r><t>ный</t></r><rPh><t>фонетика</t></rPh></si>"
        "<si><t>строка_x000D_вторая</t></si>"
        "</sst>"
    )
    data = xlsx(
        [SheetSpec("Лист1", {"A1": "Главный", "A3": "строка_x000D_вторая"})],
        shared_strings_xml=shared,
    )
    lines = _lines(xlsx_to_markdown(data))
    assert lines == ["Главный", "строка", "вторая"]


def test_container_checks() -> None:
    with pytest.raises(XlsxError) as error:
        check_container(b"%PDF-1.7")
    assert error.value.code == "format_mismatch"

    docx = io.BytesIO()
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<w:document/>")
    with pytest.raises(XlsxError) as error:
        check_container(docx.getvalue())
    assert error.value.code == "format_mismatch"

    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    with pytest.raises(XlsxError) as error:
        check_container(ole + "EncryptedPackage".encode("utf-16-le"))
    assert error.value.code == "encrypted"
    with pytest.raises(XlsxError) as error:
        check_container(ole + b"old xls")
    assert error.value.code == "format_mismatch"

    with pytest.raises(XlsxError) as error:
        check_container(b"PK\x03\x04 broken")
    assert error.value.code == "corrupted"


def test_zip_bomb_is_rejected() -> None:
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
        archive.writestr("xl/worksheets/sheet1.xml", " " * 10_000_000)
    with pytest.raises(XlsxError) as error:
        check_container(bomb.getvalue())
    assert error.value.code == "archive_too_large"


def test_doctype_is_rejected_as_corrupted() -> None:
    data = xlsx([SheetSpec("Лист1", {"A1": "x"})])
    source = zipfile.ZipFile(io.BytesIO(data))
    patched = io.BytesIO()
    with zipfile.ZipFile(patched, "w") as archive:
        for item in source.infolist():
            content = source.read(item)
            if item.filename == "xl/sharedStrings.xml":
                content = content.replace(
                    b"<sst", b'<!DOCTYPE sst [<!ENTITY a "aaaa">]><sst', 1
                )
            archive.writestr(item, content)
    with pytest.raises(XlsxError) as error:
        read_workbook(patched.getvalue())
    assert error.value.code == "corrupted"


def test_too_many_cells(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xlsx_module, "MAX_CELLS", 3)
    data = xlsx([SheetSpec("Лист1", {f"A{i}": i for i in range(1, 6)})])
    with pytest.raises(XlsxError) as error:
        read_workbook(data)
    assert error.value.code == "document_too_large"


def test_huge_merge_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xlsx_module, "MAX_CELLS", 100)
    data = xlsx([SheetSpec("Лист1", {"A1": "x", "Z500": "y"}, merges=["A1:Z500"])])
    with pytest.raises(XlsxError) as error:
        xlsx_to_markdown(data)
    assert error.value.code == "document_too_large"


def test_expat_is_protected_from_entity_expansion() -> None:
    # Защита от «billion laughs» в expat — с 2.4.1 (docstring xlsx.py).
    assert pyexpat.version_info >= (2, 4, 1)


def test_empty_workbook_gives_empty_markdown() -> None:
    assert xlsx_to_markdown(xlsx([SheetSpec("Лист1", {})])) == ""
