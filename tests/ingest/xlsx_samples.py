# ruff: noqa: E501 — URI пространств имён OOXML длиннее строки.
"""Книги .xlsx для тестов, собранные в коде — без бинарников в git.

Ячейка задаётся значением: str — общая строка, int/float — число без
формата, Num — число с кодом формата, Formula — формула с сохранённым
результатом или без, Bool, Err, Inline — строка прямо в ячейке.
"""

import io
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from xml.sax.saxutils import escape, quoteattr


@dataclass(frozen=True)
class Num:
    value: float
    code: str


@dataclass(frozen=True)
class Formula:
    cached: float | str | None
    code: str = "General"


@dataclass(frozen=True)
class Bool:
    value: bool


@dataclass(frozen=True)
class Err:
    value: str = "#N/A"


@dataclass(frozen=True)
class Inline:
    text: str


Cell = str | int | float | Num | Formula | Bool | Err | Inline


@dataclass
class SheetSpec:
    name: str
    cells: dict[str, Cell]
    merges: list[str] = field(default_factory=list)
    state: str = "visible"


_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def xlsx(
    sheets: list[SheetSpec],
    *,
    date1904: bool = False,
    shared_strings_xml: str | None = None,
) -> bytes:
    strings: list[str] = []
    codes: list[str] = []

    def string_index(text: str) -> int:
        if text not in strings:
            strings.append(text)
        return strings.index(text)

    def style_index(code: str) -> int:
        if code == "General":
            return 0
        if code not in codes:
            codes.append(code)
        return codes.index(code) + 1

    sheet_parts = [_sheet_xml(sheet, string_index, style_index) for sheet in sheets]

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types(len(sheets)))
        archive.writestr(
            "_rels/.rels",
            f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{_PKG_REL}">'
            f'<Relationship Id="rId1" Type="{_REL}/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        archive.writestr("xl/workbook.xml", _workbook_xml(sheets, date1904))
        archive.writestr("xl/_rels/workbook.xml.rels", _workbook_rels(len(sheets)))
        archive.writestr(
            "xl/sharedStrings.xml",
            shared_strings_xml
            if shared_strings_xml is not None
            else _shared_strings_xml(strings),
        )
        archive.writestr("xl/styles.xml", _styles_xml(codes))
        for index, part in enumerate(sheet_parts, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", part)
    return buffer.getvalue()


def _content_types(count: int) -> str:
    sheets = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(1, count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        f"{sheets}</Types>"
    )


def _workbook_xml(sheets: list[SheetSpec], date1904: bool) -> str:
    pr = '<workbookPr date1904="1"/>' if date1904 else "<workbookPr/>"
    items = "".join(
        f'<sheet name={quoteattr(sheet.name)} sheetId="{i}" r:id="rId{i}"'
        + (f' state="{sheet.state}"' if sheet.state != "visible" else "")
        + "/>"
        for i, sheet in enumerate(sheets, start=1)
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="{_MAIN}" xmlns:r="{_REL}">'
        f"{pr}<sheets>{items}</sheets></workbook>"
    )


def _workbook_rels(count: int) -> str:
    sheets = "".join(
        f'<Relationship Id="rId{i}" Type="{_REL}/worksheet" Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, count + 1)
    )
    n = count + 1
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{_PKG_REL}">{sheets}'
        f'<Relationship Id="rId{n}" Type="{_REL}/sharedStrings" Target="sharedStrings.xml"/>'
        f'<Relationship Id="rId{n + 1}" Type="{_REL}/styles" Target="styles.xml"/>'
        "</Relationships>"
    )


def _shared_strings_xml(strings: list[str]) -> str:
    items = "".join(
        f'<si><t xml:space="preserve">{escape(text)}</t></si>' for text in strings
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><sst xmlns="{_MAIN}" count="{len(strings)}">'
        f"{items}</sst>"
    )


def _styles_xml(codes: list[str]) -> str:
    formats = "".join(
        f'<numFmt numFmtId="{164 + i}" formatCode={quoteattr(code)}/>'
        for i, code in enumerate(codes)
    )
    xfs = '<xf numFmtId="0"/>' + "".join(
        f'<xf numFmtId="{164 + i}" applyNumberFormat="1"/>' for i in range(len(codes))
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="{_MAIN}">'
        f'<numFmts count="{len(codes)}">{formats}</numFmts>'
        # Стили-образцы: их xf не относятся к ячейкам и не должны сбить индекс.
        '<cellStyleXfs count="1"><xf numFmtId="9"/></cellStyleXfs>'
        f'<cellXfs count="{len(codes) + 1}">{xfs}</cellXfs></styleSheet>'
    )


def _row_number(ref: str) -> int:
    return int("".join(ch for ch in ref if ch.isdigit()))


def _sheet_xml(
    sheet: SheetSpec,
    string_index: Callable[[str], int],
    style_index: Callable[[str], int],
) -> str:
    rows: dict[int, list[str]] = {}
    for ref, value in sheet.cells.items():
        rows.setdefault(_row_number(ref), []).append(
            _cell_xml(ref, value, string_index, style_index)
        )
    body = "".join(
        f'<row r="{number}">{"".join(cells)}</row>'
        for number, cells in sorted(rows.items())
    )
    merges = ""
    if sheet.merges:
        merges = (
            f'<mergeCells count="{len(sheet.merges)}">'
            + "".join(f'<mergeCell ref="{ref}"/>' for ref in sheet.merges)
            + "</mergeCells>"
        )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{_MAIN}" xmlns:r="{_REL}">'
        f"<sheetData>{body}</sheetData>{merges}</worksheet>"
    )


def _cell_xml(
    ref: str,
    value: Cell,
    string_index: Callable[[str], int],
    style_index: Callable[[str], int],
) -> str:
    if isinstance(value, str):
        return f'<c r="{ref}" t="s"><v>{string_index(value)}</v></c>'
    if isinstance(value, Bool):
        return f'<c r="{ref}" t="b"><v>{int(value.value)}</v></c>'
    if isinstance(value, Err):
        return f'<c r="{ref}" t="e"><v>{escape(value.value)}</v></c>'
    if isinstance(value, Inline):
        return f'<c r="{ref}" t="inlineStr"><is><t>{escape(value.text)}</t></is></c>'
    if isinstance(value, Num):
        return f'<c r="{ref}" s="{style_index(value.code)}"><v>{value.value!r}</v></c>'
    if isinstance(value, Formula):
        style = style_index(value.code)
        if value.cached is None:
            return f'<c r="{ref}" s="{style}"><f>SUM(A1:A2)</f></c>'
        if isinstance(value.cached, str):
            return f'<c r="{ref}" t="str"><f>A1</f><v>{escape(value.cached)}</v></c>'
        return f'<c r="{ref}" s="{style}"><f>SUM(A1:A2)</f><v>{value.cached!r}</v></c>'
    return f'<c r="{ref}"><v>{value!r}</v></c>'
