"""Таблицы Excel (.xlsx) → Markdown (Р-5: первый из новых форматов).

Решение Артёма 29.09 (Р-5): .xlsx, .pptx, .doc — по одному; следующий
формат — только когда предыдущий дошёл до приемлемого качества. Здесь —
только разбор файла. Приём формата в `extract.py`, песочница, сообщения
админу и коннекторы — бэкенд (BH-33 в `docs/backend-handoff.md`).

Без сторонних библиотек: .xlsx — zip с XML (ECMA-376), читаем его
`zipfile` и `xml.etree.ElementTree.iterparse` — лист потоком, без дерева
в памяти. ElementTree не ходит за внешними сущностями, expat ≥ 2.4.1
защищён от «billion laughs» (проверяется тестом); DTD Excel не пишет —
часть с `<!DOCTYPE` отклоняется как повреждённая.

Как книга превращается в Markdown. Цель — чтобы после `preprocess`
каждая строка таблицы стала самодостаточной строкой «ключ: значение; …»,
а крошки чанка называли лист и таблицу:

1. Лист — заголовок `#`, в порядке книги. Скрытые листы пропускаются
   (справочники выпадающих списков, служебные расчёты). Стандартные
   имена («Лист1», «Sheet1») заголовком не становятся: в крошках они шум.
2. Объединённые ячейки: значение копируется во все ячейки диапазона.
   Иначе строка, у которой «Европа» объединена на пять строк вниз,
   теряет регион.
3. Блоки — строки подряд без пустой строки между ними. Строка с одним
   значением — текст, с двумя и больше — строка таблицы.
4. Короткий текст прямо над таблицей — её заголовок, на уровень ниже
   листа. На нём держатся крошки всех чанков таблицы.
5. Шапка — первая строка таблицы, если в ней в основном текст, а не
   числа. Объединение по горизонтали в шапке («Суточные» над «до 10
   дней» и «свыше 10 дней») — шапка в две строки, ключ «Суточные — до 10
   дней».
6. Строка-группа внутри таблицы («Европа», «Отдел продаж») —
   подзаголовок, после него таблица продолжается с той же шапкой. Группа —
   одно значение, объединённое на несколько столбцов, или одно значение в
   первом столбце таблицы шире двух столбцов (`_is_group`).
7. Значения — как их показывает Excel, по формату ячейки: числа с
   запятой («1,5»), проценты «15%», даты «01.10.2026», ИСТИНА/ЛОЖЬ —
   «да»/«нет», ошибки (#Н/Д) — пусто. Формула — её сохранённый результат;
   если результата нет (файл собран программой, а не Excel), ячейка пустая
   и попадает в `Workbook.formulas_without_value`.
8. Скрытые строки и столбцы остаются: так не теряются строки, скрытые
   автофильтром, — это данные.
"""

import io
import re
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from posixpath import basename, dirname, join, normpath
from typing import Literal
from xml.etree.ElementTree import Element, ParseError, iterparse

MAX_UNCOMPRESSED = 200 * 1024 * 1024
MAX_ENTRIES = 5000
MAX_COMPRESSION_RATIO = 200
"""Как у docx в `extract.py`: zip-бомба — 40 КБ, которые распаковываются
в гигабайты."""

MAX_CELLS = 500_000
"""Непустых ячеек на книгу (вместе с размноженными объединениями). Выше —
`document_too_large`: текст такой книги всё равно упрётся в
`MAX_EXTRACTED_CHARS` бэкенда (2 млн символов)."""

TITLE_MAX_CHARS = 150
"""Текст длиннее — абзац, а не заголовок таблицы или группы."""

_XML_HEAD_BYTES = 4096

_Event = Literal["start", "end"]


class XlsxError(Exception):
    """Книгу нельзя принять. code — те же коды, что у `ExtractionError`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class Sheet:
    name: str
    cells: dict[tuple[int, int], str]
    """(строка, столбец) с 1 → текст ячейки, только непустые."""
    merges: list[tuple[int, int, int, int]] = field(default_factory=list)
    """Объединения: первая строка, первый столбец, последняя, последний."""


@dataclass
class Workbook:
    sheets: list[Sheet]
    """Видимые листы в порядке книги."""
    hidden_sheets: list[str] = field(default_factory=list)
    formulas_without_value: int = 0


def xlsx_to_markdown(data: bytes) -> str:
    """Файл .xlsx → Markdown до preprocess. Вызывать в песочнице."""
    return workbook_to_markdown(read_workbook(data))


# --- Контейнер ---------------------------------------------------------------


def check_container(data: bytes) -> None:
    """Дешёвые проверки до разбора: сигнатура, шифрование, zip-бомба."""
    if data.startswith(b"\xd0\xcf\x11\xe0"):
        # OLE2: либо книга с паролем (EncryptedPackage), либо старый .xls.
        if "EncryptedPackage".encode("utf-16-le") in data:
            raise XlsxError("encrypted")
        raise XlsxError("format_mismatch")
    if not data.startswith(b"PK\x03\x04"):
        raise XlsxError("format_mismatch")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            # Сначала размеры: связи читаем уже из проверенного архива.
            if len(entries) > MAX_ENTRIES:
                raise XlsxError("archive_too_large")
            total = 0
            for entry in entries:
                total += entry.file_size
                ratio = entry.file_size / max(entry.compress_size, 1)
                if total > MAX_UNCOMPRESSED or ratio > MAX_COMPRESSION_RATIO:
                    raise XlsxError("archive_too_large")

            names = {entry.filename for entry in entries}
            if "[Content_Types].xml" not in names:
                raise XlsxError("format_mismatch")
            if _workbook_part(archive, names) not in names:
                # zip, но не книга Excel (docx, pptx, просто архив).
                raise XlsxError("format_mismatch")
    except (zipfile.BadZipFile, zlib.error, EOFError, ParseError, ValueError) as exc:
        raise XlsxError("corrupted") from exc


# --- Разбор книги --------------------------------------------------------------


@dataclass(frozen=True)
class _SheetRef:
    name: str
    state: str
    part: str | None


def read_workbook(data: bytes) -> Workbook:
    """Книга → видимые листы с текстом ячеек."""
    check_container(data)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return _read_archive(archive)
    except XlsxError:
        raise
    except (
        zipfile.BadZipFile,
        zlib.error,
        EOFError,
        ParseError,
        ValueError,
        KeyError,
        IndexError,
    ) as exc:
        raise XlsxError("corrupted") from exc


def _read_archive(archive: zipfile.ZipFile) -> Workbook:
    names = set(archive.namelist())
    workbook_part = _workbook_part(archive, names)
    relations = _relations(archive, workbook_part, names)
    refs, date1904 = _sheet_refs(archive, workbook_part, relations)

    strings_part = _first_target(relations, "/sharedStrings")
    strings = _shared_strings(archive, strings_part) if strings_part in names else []
    styles_part = _first_target(relations, "/styles")
    formats = _cell_formats(archive, styles_part) if styles_part in names else []

    workbook = Workbook(sheets=[])
    budget = MAX_CELLS
    for ref in refs:
        if ref.part is None or ref.part not in names:
            continue
        if ref.state in ("hidden", "veryHidden"):
            workbook.hidden_sheets.append(ref.name)
            continue
        sheet, empty_formulas = _read_sheet(
            archive, ref, strings, formats, date1904=date1904, budget=budget
        )
        budget -= len(sheet.cells)
        workbook.formulas_without_value += empty_formulas
        workbook.sheets.append(sheet)
    return workbook


def _local(tag: str) -> str:
    """Имя без пространства имён: Strict OOXML и Transitional — одно и то же."""
    return tag.rsplit("}", 1)[-1]


def _attr(element: Element, local: str) -> str | None:
    """Атрибут по локальному имени (r:id в любом пространстве имён)."""
    for key, value in element.attrib.items():
        if _local(key) == local:
            return value
    return None


def _parse(
    archive: zipfile.ZipFile, part: str, events: tuple[_Event, ...] = ("end",)
) -> Iterator[tuple[str, Element]]:
    with archive.open(part) as stream:
        head = stream.read(_XML_HEAD_BYTES)
    if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
        raise XlsxError("corrupted")
    with archive.open(part) as stream:
        # Безопасность разбора — в docstring модуля.
        yield from iterparse(stream, events=events)  # noqa: S314


@dataclass(frozen=True)
class _Relation:
    type: str
    id: str
    target: str
    """Путь цели внутри архива."""


def _workbook_part(archive: zipfile.ZipFile, names: set[str]) -> str:
    for relation in _relations(archive, "", names):
        if relation.type.endswith("/officeDocument"):
            return relation.target
    return "xl/workbook.xml"


def _relations(archive: zipfile.ZipFile, part: str, names: set[str]) -> list[_Relation]:
    """Связи части (`""` — корень пакета); внешние ссылки пропускаются."""
    rels_part = join(dirname(part), "_rels", basename(part) + ".rels")
    if rels_part not in names:
        return []
    relations: list[_Relation] = []
    for _, element in _parse(archive, rels_part):
        if _local(element.tag) != "Relationship":
            continue
        if element.get("TargetMode") == "External":
            continue
        target = element.get("Target", "")
        if target.startswith("/"):
            path = target.lstrip("/")
        else:
            path = normpath(join(dirname(part), target))
        relations.append(
            _Relation(element.get("Type", ""), element.get("Id", ""), path)
        )
    return relations


def _first_target(relations: list[_Relation], type_suffix: str) -> str:
    for relation in relations:
        if relation.type.endswith(type_suffix):
            return relation.target
    return ""


def _sheet_refs(
    archive: zipfile.ZipFile, workbook_part: str, relations: list[_Relation]
) -> tuple[list[_SheetRef], bool]:
    worksheets = {
        relation.id: relation.target
        for relation in relations
        if relation.type.endswith("/worksheet")
    }
    refs: list[_SheetRef] = []
    date1904 = False
    for _, element in _parse(archive, workbook_part):
        name = _local(element.tag)
        if name == "workbookPr":
            date1904 = element.get("date1904", "").lower() in ("1", "true")
        elif name == "sheet":
            rel_id = _attr(element, "id") or ""
            refs.append(
                _SheetRef(
                    name=(element.get("name") or "").strip(),
                    state=element.get("state", "visible"),
                    # Лист диаграммы — тоже «sheet», но связь другого типа.
                    part=worksheets.get(rel_id),
                )
            )
    return refs, date1904


_ESCAPED_CHAR = re.compile(r"_x([0-9A-Fa-f]{4})_")


def _unescape(text: str) -> str:
    """Excel кодирует управляющие символы как _x000D_."""
    return _ESCAPED_CHAR.sub(lambda m: chr(int(m.group(1), 16)), text)


def _rich_text(element: Element) -> str:
    """Текст <si> или <is>: <t> и <r><t>, без фонетики <rPh>."""
    parts: list[str] = []
    for child in element:
        name = _local(child.tag)
        if name == "t":
            parts.append(child.text or "")
        elif name == "r":
            parts.extend(t.text or "" for t in child if _local(t.tag) == "t")
    return _unescape("".join(parts))


def _shared_strings(archive: zipfile.ZipFile, part: str) -> list[str]:
    strings: list[str] = []
    for _, element in _parse(archive, part):
        if _local(element.tag) == "si":
            strings.append(_rich_text(element))
            element.clear()
    return strings


def _cell_formats(archive: zipfile.ZipFile, part: str) -> list[str]:
    """Код формата числа для каждого стиля ячейки (индекс = атрибут s)."""
    custom: dict[int, str] = {}
    format_ids: list[int] = []
    in_cell_xfs = False
    for event, element in _parse(archive, part, ("start", "end")):
        name = _local(element.tag)
        if event == "start":
            in_cell_xfs = in_cell_xfs or name == "cellXfs"
            continue
        if name == "numFmt":
            custom[int(element.get("numFmtId", "0"))] = element.get("formatCode", "")
        elif name == "xf" and in_cell_xfs:
            format_ids.append(int(element.get("numFmtId", "0")))
        elif name == "cellXfs":
            in_cell_xfs = False
    return [custom.get(i, _BUILTIN_FORMATS.get(i, "General")) for i in format_ids]


_CELL_REF = re.compile(r"^([A-Za-z]{1,3})(\d+)$")


def _parse_ref(ref: str) -> tuple[int, int]:
    match = _CELL_REF.match(ref)
    if match is None:
        raise ValueError(f"bad cell reference {ref!r}")
    column = 0
    for char in match.group(1).upper():
        column = column * 26 + ord(char) - ord("A") + 1
    return int(match.group(2)), column


def _read_sheet(
    archive: zipfile.ZipFile,
    ref: _SheetRef,
    strings: list[str],
    formats: list[str],
    *,
    date1904: bool,
    budget: int,
) -> tuple[Sheet, int]:
    assert ref.part is not None  # noqa: S101 — проверено вызывающим
    sheet = Sheet(name=ref.name, cells={})
    empty_formulas = 0
    row = column = 0
    for event, element in _parse(archive, ref.part, ("start", "end")):
        name = _local(element.tag)
        if event == "start":
            if name == "row":
                number = element.get("r")
                row = int(number) if number else row + 1
                column = 0
            continue
        if name == "c":
            position = element.get("r")
            row, column = _parse_ref(position) if position else (row, column + 1)
            text, empty_formula = _cell_text(element, strings, formats, date1904)
            empty_formulas += empty_formula
            if text:
                sheet.cells[(row, column)] = text
                if len(sheet.cells) > budget:
                    raise XlsxError("document_too_large")
            element.clear()
        elif name == "row":
            element.clear()
        elif name == "mergeCell":
            first, _, last = (element.get("ref") or "").partition(":")
            if first and last:
                (r1, c1), (r2, c2) = _parse_ref(first), _parse_ref(last)
                sheet.merges.append(
                    (min(r1, r2), min(c1, c2), max(r1, r2), max(c1, c2))
                )
    return sheet, empty_formulas


# --- Значения ячеек ------------------------------------------------------------


def _cell_text(
    element: Element, strings: list[str], formats: list[str], date1904: bool
) -> tuple[str, int]:
    """Текст ячейки как в Excel и 1, если это формула без результата."""
    kind = element.get("t", "n")
    value: str | None = None
    has_formula = False
    for child in element:
        name = _local(child.tag)
        if name == "v":
            value = child.text
        elif name == "f":
            has_formula = True
        elif name == "is":
            return _rich_text(child).strip(), 0
    if value is None:
        return "", int(has_formula)

    if kind == "s":
        index = int(value)
        return (strings[index].strip() if 0 <= index < len(strings) else ""), 0
    if kind in ("str", "inlineStr"):
        return _unescape(value).strip(), 0
    if kind == "b":
        return ("да" if value.strip() == "1" else "нет"), 0
    if kind == "e":
        return "", 0

    style = element.get("s")
    code = formats[int(style)] if style and int(style) < len(formats) else "General"
    if kind == "d":
        return _format_iso_date(value, code), 0
    try:
        number = float(value)
    except ValueError:
        return value.strip(), 0
    return format_number(number, code, date1904=date1904), 0


_BUILTIN_FORMATS: dict[int, str] = {
    0: "General",
    1: "0",
    2: "0.00",
    3: "#,##0",
    4: "#,##0.00",
    9: "0%",
    10: "0.00%",
    11: "0.00E+00",
    12: "# ?/?",
    13: "# ??/??",
    14: "dd.mm.yyyy",
    15: "d-mmm-yy",
    16: "d-mmm",
    17: "mmm-yy",
    18: "h:mm AM/PM",
    19: "h:mm:ss AM/PM",
    20: "h:mm",
    21: "h:mm:ss",
    22: "dd.mm.yyyy h:mm",
    37: "#,##0 ;(#,##0)",
    38: "#,##0 ;[Red](#,##0)",
    39: "#,##0.00;(#,##0.00)",
    40: "#,##0.00;[Red](#,##0.00)",
    45: "mm:ss",
    46: "[h]:mm:ss",
    47: "mmss.0",
    48: "##0.0E+0",
    49: "@",
    # Даты национальных форматов (27–36, 50–58): нам важно только «дата».
    **{i: "dd.mm.yyyy" for i in (*range(27, 37), *range(50, 59))},
}

_QUOTED = re.compile(r'"[^"]*"|\\.')
_BRACKETS = re.compile(r"\[(?!h\]|hh\]|m\]|mm\]|s\]|ss\])[^\]]*\]", re.IGNORECASE)
_DECIMALS = re.compile(r"\.([0#?]+)")
_MAX_EXCEL_SERIAL = 2_958_465  # 31.12.9999


@dataclass(frozen=True)
class _Format:
    kind: str  # general | text | number | percent | date | datetime | time
    decimals: int | None = None
    currency: str = ""


def _classify(code: str) -> _Format:
    """Код формата Excel → что показывать. Смотрим положительную секцию."""
    section = code.split(";", 1)[0]
    plain = _BRACKETS.sub("", _QUOTED.sub("", section)).lower().strip()
    if not plain or plain == "general":
        return _Format("general")
    if plain == "@":
        return _Format("text")
    has_date = "y" in plain or "d" in plain
    has_time = "h" in plain or "s" in plain
    if has_date and has_time:
        return _Format("datetime")
    if has_date or ("m" in plain and not has_time and "e" not in plain):
        return _Format("date")
    if has_time:
        return _Format("time")
    decimals_match = _DECIMALS.search(plain)
    decimals = len(decimals_match.group(1)) if decimals_match else 0
    if "%" in plain:
        return _Format("percent", decimals)
    if "e+" in plain or "e-" in plain or "?/" in plain:
        return _Format("general")
    return _Format("number", decimals, _currency(section))


def _currency(code: str) -> str:
    lowered = code.lower()
    if "₽" in code or "руб" in lowered or "р." in lowered:
        return "₽"
    if "€" in code:
        return "€"
    # «[$-419]» — только тег языка, «[$$-409]» и «$» — доллар.
    if re.search(r"\$(?!-)", code.replace("[$-", "")):
        return "$"
    return ""


def format_number(number: float, code: str, *, date1904: bool = False) -> str:
    """Число из ячейки → текст, как его показывает Excel (без разрядов)."""
    fmt = _classify(code)
    if fmt.kind in ("date", "datetime", "time") and 0 <= number <= _MAX_EXCEL_SERIAL:
        moment = _from_serial(number, date1904)
        if fmt.kind == "date":
            return moment.strftime("%d.%m.%Y")
        if fmt.kind == "time":
            return moment.strftime("%H:%M")
        return moment.strftime("%d.%m.%Y %H:%M")
    if fmt.kind == "percent":
        return _decimal_text(number * 100, fmt.decimals) + "%"
    if fmt.kind == "number":
        text = _decimal_text(number, fmt.decimals)
        return f"{text} {fmt.currency}" if fmt.currency else text
    return _decimal_text(number, None)


def _from_serial(serial: float, date1904: bool) -> datetime:
    # Система 1900: день 1 — 01.01.1900, плюс фиктивное 29.02.1900 —
    # отсюда 30.12.1899. В системе 1904 день 0 — 01.01.1904.
    epoch = datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 30)
    # Округление до секунды: 0.5 суток в float — не ровно 12:00:00.
    return epoch + timedelta(seconds=round(serial * 86400))


def _decimal_text(number: float, decimals: int | None) -> str:
    if decimals is None:
        if number.is_integer() and abs(number) < 1e15:
            text = str(int(number))
        else:
            text = f"{number:.10g}"
    else:
        text = f"{number:.{decimals}f}"
    if text in ("-0", "-0.0") or re.fullmatch(r"-0\.0+", text):
        text = text[1:]
    return text.replace(".", ",")


def _format_iso_date(value: str, code: str) -> str:
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return value.strip()
    kind = _classify(code).kind
    if kind == "time":
        return moment.strftime("%H:%M")
    if kind == "datetime" or (moment.hour, moment.minute) != (0, 0):
        return moment.strftime("%d.%m.%Y %H:%M")
    return moment.strftime("%d.%m.%Y")


# --- Книга → Markdown ------------------------------------------------------------

_DEFAULT_SHEET_NAME = re.compile(
    r"^(?:лист|sheet|feuil|tabelle|hoja|foglio|planilha)\s*\d*$", re.IGNORECASE
)
_NUMERIC = re.compile(
    r"^[-+−]?\d[\d\s]*(?:[.,]\d+)?\s*(?:%|₽|\$|€)?$"
    r"|^\d{1,2}\.\d{1,2}\.\d{4}(?: \d{1,2}:\d{2})?$"
    r"|^\d{1,2}:\d{2}$"
)


def workbook_to_markdown(workbook: Workbook) -> str:
    blocks: list[str] = []
    for sheet in workbook.sheets:
        named = bool(sheet.name) and not _DEFAULT_SHEET_NAME.match(sheet.name)
        body = _sheet_lines(sheet, base_level=1 if named else 0)
        if not body:
            continue
        lines = [_heading(1, sheet.name), ""] if named else []
        blocks.append("\n".join(lines + body).strip())
    return "\n\n".join(blocks).strip() + "\n" if blocks else ""


@dataclass
class _Grid:
    rows: dict[int, dict[int, str]]
    merge_of: dict[tuple[int, int], int]

    def units(self, row: int) -> int:
        """Сколько разных значений в строке: объединение считается одним."""
        seen: set[int] = set()
        count = 0
        for column in self.rows[row]:
            merge = self.merge_of.get((row, column))
            if merge is None:
                count += 1
            elif merge not in seen:
                seen.add(merge)
                count += 1
        return count

    def text(self, row: int) -> str:
        """Значение строки из одного значения."""
        cells = self.rows[row]
        return cells[min(cells)]

    def has_horizontal_merge(self, row: int) -> bool:
        columns = sorted(self.rows[row])
        return any(
            self.merge_of.get((row, a)) is not None
            and self.merge_of.get((row, a)) == self.merge_of.get((row, b))
            for a, b in zip(columns, columns[1:], strict=False)
            if b == a + 1
        )


def _grid(sheet: Sheet) -> _Grid:
    cells = dict(sheet.cells)
    merge_of: dict[tuple[int, int], int] = {}
    if cells:
        max_row = max(r for r, _ in cells)
        max_column = max(c for _, c in cells)
        budget = MAX_CELLS
        for index, (r1, c1, r2, c2) in enumerate(sheet.merges):
            value = sheet.cells.get((r1, c1))
            r2, c2 = min(r2, max_row), min(c2, max_column)
            if not value or (r1, c1) == (r2, c2) or r2 < r1 or c2 < c1:
                continue
            budget -= (r2 - r1 + 1) * (c2 - c1 + 1)
            if budget < 0:
                raise XlsxError("document_too_large")
            for r in range(r1, r2 + 1):
                for c in range(c1, c2 + 1):
                    cells[(r, c)] = value
                    merge_of[(r, c)] = index
    rows: dict[int, dict[int, str]] = {}
    for (r, c), value in sorted(cells.items()):
        rows.setdefault(r, {})[c] = value
    return _Grid(rows=rows, merge_of=merge_of)


def _blocks(rows: list[int]) -> list[list[int]]:
    """Строки подряд; пустая строка между ними — граница блока."""
    blocks: list[list[int]] = []
    for row in rows:
        if blocks and row == blocks[-1][-1] + 1:
            blocks[-1].append(row)
        else:
            blocks.append([row])
    return blocks


def _sheet_lines(sheet: Sheet, *, base_level: int) -> list[str]:
    grid = _grid(sheet)
    out: list[str] = []
    pending_title: str | None = None

    for block in _blocks(sorted(grid.rows)):
        data = [i for i, row in enumerate(block) if grid.units(row) >= 2]
        if not data:
            texts = [grid.text(row) for row in block]
            if pending_title is not None:
                _paragraph(out, pending_title)
            for text in texts[:-1]:
                _paragraph(out, text)
            pending_title = texts[-1] if _is_title(texts[-1]) else None
            if pending_title is None:
                _paragraph(out, texts[-1])
            continue

        first, last = data[0], data[-1]
        leading = [grid.text(row) for row in block[:first]]
        if leading:
            if pending_title is not None:
                _paragraph(out, pending_title)
            for text in leading[:-1]:
                _paragraph(out, text)
            title = leading[-1] if _is_title(leading[-1]) else None
            if title is None:
                _paragraph(out, leading[-1])
        else:
            title = pending_title
        pending_title = None

        group_level = base_level + 1
        if title is not None:
            out.extend([_heading(base_level + 1, title), ""])
            group_level = base_level + 2
        _table(out, grid, block[first : last + 1], group_level)
        for row in block[last + 1 :]:
            _paragraph(out, grid.text(row))

    if pending_title is not None:
        _paragraph(out, pending_title)
    while out and not out[-1]:
        out.pop()
    return out


def _table(out: list[str], grid: _Grid, rows: list[int], group_level: int) -> None:
    data_rows = [row for row in rows if grid.units(row) >= 2]
    columns = sorted({c for row in data_rows for c in grid.rows[row]})
    header = data_rows[0]
    keys: list[str] | None = None
    body = rows[rows.index(header) + 1 :]

    if _is_header(grid.rows[header], columns):
        header_rows = [header]
        second = body[0] if body else None
        if (
            second is not None
            and grid.has_horizontal_merge(header)
            and grid.units(second) >= 2
            and _is_header(grid.rows[second], columns)
        ):
            header_rows.append(second)
            body = body[1:]
        keys = []
        for column in columns:
            parts: list[str] = []
            for row in header_rows:
                part = _single_line(grid.rows[row].get(column, ""))
                if part and part not in parts:
                    parts.append(part)
            keys.append(" — ".join(parts))
        if not any(keys):
            keys = None
    else:
        body = rows[rows.index(header) :]

    chunk: list[list[str]] = []

    def flush() -> None:
        if not chunk and keys is None:
            return
        if keys is not None:
            out.append(_pipe(keys))
            out.append(_pipe(["---"] * len(keys)))
        out.extend(_pipe(cells) for cells in chunk)
        out.append("")
        chunk.clear()

    for row in body:
        if not _is_group(grid, row, columns):
            chunk.append([grid.rows[row].get(c, "") for c in columns])
            continue
        text = grid.text(row)
        if chunk:
            flush()
        if _is_title(text):
            out.extend([_heading(group_level, text), ""])
        else:
            _paragraph(out, text)
    if chunk or (keys is not None and not body):
        flush()


def _is_header(cells: dict[int, str], columns: list[int]) -> bool:
    values = [cells[c] for c in columns if cells.get(c)]
    if not values or any(len(v) > TITLE_MAX_CHARS for v in values):
        return False
    textual = sum(1 for v in values if not _NUMERIC.match(v))
    return textual * 2 >= len(values)


def _is_group(grid: _Grid, row: int, columns: list[int]) -> bool:
    """Строка-группа внутри таблицы, а не строка с пустыми ячейками.

    Одно значение, объединённое на несколько столбцов, — группа. Без
    объединения группой считается только значение в первом столбце
    таблицы из трёх и более столбцов: в таблице «Кто | Телефон» строка
    «Петров» без телефона — данные, а не раздел.
    """
    if grid.units(row) >= 2:
        return False
    if grid.has_horizontal_merge(row):
        return True
    return len(columns) >= 3 and min(grid.rows[row]) == columns[0]


def _is_title(text: str) -> bool:
    return len(text.splitlines()) == 1 and len(text) <= TITLE_MAX_CHARS


def _single_line(text: str) -> str:
    return " ".join(text.split())


def _pipe(cells: list[str]) -> str:
    return "| " + " | ".join(_single_line(c).replace("|", "\\|") for c in cells) + " |"


def _heading(level: int, text: str) -> str:
    return "#" * min(max(level, 1), 6) + " " + _single_line(text)


def _paragraph(out: list[str], text: str) -> None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines:
        out.extend([*lines, ""])
