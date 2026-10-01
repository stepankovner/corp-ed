"""Презентации PowerPoint (.pptx) → Markdown (Р-5: второй из новых форматов).

Решение Артёма 29.09 (Р-5): форматы по одному; .xlsx принят 01.10, этот —
следующий. Здесь только разбор файла; приём формата — бэкенд (BH-34 в
`docs/backend-handoff.md`). Пакет, связи и безопасный разбор XML — общие с
.xlsx (`ingest/ooxml.py`), сторонних библиотек нет.

Как презентация превращается в Markdown — так, чтобы у каждого фрагмента
в крошках стоял заголовок его слайда:

1. Слайд — заголовок `#`: текст заполнителя заголовка. Нет заголовка —
   первая короткая строка слайда, иначе «Слайд N». Скрытые слайды
   пропускаются, их номера — в `Presentation.hidden_slides`.
2. Текст фигур — по абзацам; уровень списка — отступом и «- ».
   Порядок фигур — сверху вниз и слева направо, если у всех есть
   координаты, иначе — как в файле. Колонтитулы, дата и номер слайда
   отбрасываются.
3. Таблицы — по правилам листа Excel (`xlsx.table_lines`): объединённые
   ячейки размножаются, шапка в две строки склеивается, строка-группа —
   подзаголовок. Дальше `preprocess` делает «ключ: значение; …».
4. Диаграммы — таблица из сохранённых в файле значений: категории ×
   ряды, числа по формату ряда; над ней — «Диаграмма: <название>».
5. SmartArt — текст узлов списком.
6. Заметки докладчика — после содержимого слайда, строкой «Заметки
   докладчика:» и текстом: в них часто то, что говорят вслух, а на слайде
   нет.
"""

import re
import zipfile
from contextlib import suppress
from dataclasses import dataclass, field
from xml.etree.ElementTree import Element

from corp_ed.ingest.ooxml import (
    READ_ERRORS,
    OfficeFileError,
    check_package,
    child,
    children,
    first_target,
    local,
    main_part,
    open_archive,
    parse,
    parse_tree,
    rel_id,
    relations,
)
from corp_ed.ingest.xlsx import Sheet, format_number, table_lines

MAX_SLIDES = 2000
"""Больше — `document_too_large`."""

TITLE_MAX_CHARS = 120
"""Первая строка слайда длиннее — не заголовок."""

_SKIP_PLACEHOLDERS = frozenset({"sldNum", "ftr", "dt", "hdr", "sldImg"})
_TITLE_PLACEHOLDERS = frozenset({"title", "ctrTitle"})


@dataclass
class Slide:
    number: int
    title: str
    blocks: list[str] = field(default_factory=list)
    """Куски Markdown: абзацы фигуры, таблица, диаграмма, SmartArt."""
    notes: list[str] = field(default_factory=list)


@dataclass
class Presentation:
    slides: list[Slide]
    """Видимые слайды в порядке показа."""
    hidden_slides: list[int] = field(default_factory=list)
    tables: int = 0
    charts: int = 0
    diagrams: int = 0


def pptx_to_markdown(data: bytes) -> str:
    """Файл .pptx → Markdown до preprocess. Вызывать в песочнице."""
    return presentation_to_markdown(read_presentation(data))


def check_container(data: bytes) -> None:
    """Дешёвые проверки до разбора: сигнатура, пароль, zip-бомба, тип."""
    check_package(data, "presentation")


def read_presentation(data: bytes) -> Presentation:
    check_container(data)
    try:
        with open_archive(data) as archive:
            return _read_archive(archive)
    except OfficeFileError:
        raise
    except READ_ERRORS as exc:
        raise OfficeFileError("corrupted") from exc


# --- Пакет -------------------------------------------------------------------------


@dataclass
class _Context:
    archive: zipfile.ZipFile
    names: set[str]
    targets: dict[str, str]
    """Связи слайда: r:id → часть пакета."""
    presentation: Presentation


def _read_archive(archive: zipfile.ZipFile) -> Presentation:
    names = set(archive.namelist())
    presentation_part = main_part(archive, names, "presentation")
    slide_parts = {
        relation.id: relation.target
        for relation in relations(archive, presentation_part, names)
        if relation.type.endswith("/slide")
    }
    order = [
        slide_parts[rid]
        for _, element in parse(archive, presentation_part)
        if local(element.tag) == "sldId" and (rid := rel_id(element)) in slide_parts
    ]
    if len(order) > MAX_SLIDES:
        raise OfficeFileError("document_too_large")

    presentation = Presentation(slides=[])
    for number, part in enumerate(order, start=1):
        if part not in names:
            continue
        root = parse_tree(archive, part)
        if root.get("show") in ("0", "false"):
            presentation.hidden_slides.append(number)
            continue
        found = relations(archive, part, names)
        context = _Context(
            archive=archive,
            names=names,
            targets={relation.id: relation.target for relation in found},
            presentation=presentation,
        )
        slide = _read_slide(root, number, context)
        notes_part = first_target(found, "/notesSlide")
        if notes_part in names:
            slide.notes = _notes(parse_tree(archive, notes_part))
        presentation.slides.append(slide)
    return presentation


# --- Слайд ------------------------------------------------------------------------


@dataclass
class _Item:
    role: str
    """title | body"""
    blocks: list[str]
    position: tuple[int, int] | None


def _read_slide(root: Element, number: int, context: _Context) -> Slide:
    tree = _sp_tree(root)
    items = _items(tree, context) if tree is not None else []
    titles = [i for i in items if i.role == "title" and i.blocks]
    body = [i for i in items if i.role == "body" and i.blocks]
    if body and all(i.position is not None for i in body):
        body.sort(key=lambda i: i.position or (0, 0))

    blocks = [block for item in body for block in item.blocks]
    title = " ".join(" ".join(b for t in titles for b in t.blocks).split())
    if not title and blocks:
        first, _, rest = blocks[0].partition("\n")
        if len(first) <= TITLE_MAX_CHARS and not first.lstrip().startswith(("-", "|")):
            title = first.strip()
            blocks = ([rest] if rest.strip() else []) + blocks[1:]
    return Slide(number=number, title=title or f"Слайд {number}", blocks=blocks)


def _sp_tree(root: Element) -> Element | None:
    common = child(root, "cSld")
    return child(common, "spTree") if common is not None else None


def _items(tree: Element, context: _Context) -> list[_Item]:
    items: list[_Item] = []
    for element in tree:
        name = local(element.tag)
        if name == "sp":
            item = _text_shape(element)
            if item is not None:
                items.append(item)
        elif name == "graphicFrame":
            blocks = _frame(element, context)
            if blocks:
                items.append(_Item("body", blocks, _position(element, "xfrm")))
        elif name == "grpSp":
            inner = _items(element, context)
            if inner:
                # Группа — одна фигура: её части в порядке файла.
                title = [i for i in inner if i.role == "title"]
                rest = [b for i in inner if i.role == "body" for b in i.blocks]
                items.extend(title)
                if rest:
                    items.append(_Item("body", rest, _position(element, "grpSpPr")))
    return items


def _position(element: Element, props: str) -> tuple[int, int] | None:
    """(y, x) левого верхнего угла из xfrm/off — в EMU."""
    holder = child(element, props)
    if holder is None:
        return None
    transform = holder if local(holder.tag) == "xfrm" else child(holder, "xfrm")
    offset = child(transform, "off") if transform is not None else None
    if offset is None:
        return None
    return int(offset.get("y", "0")), int(offset.get("x", "0"))


def _placeholder(element: Element) -> str | None:
    """Тип заполнителя фигуры; None — обычная фигура."""
    for props in element:
        if local(props.tag).startswith("nv"):
            nv = child(props, "nvPr")
            ph = child(nv, "ph") if nv is not None else None
            if ph is not None:
                return ph.get("type", "body")
    return None


def _text_shape(element: Element) -> _Item | None:
    kind = _placeholder(element)
    if kind in _SKIP_PLACEHOLDERS:
        return None
    body = child(element, "txBody")
    if body is None:
        return None
    lines = _paragraph_lines(body)
    if not lines:
        return None
    if kind in _TITLE_PLACEHOLDERS:
        return _Item("title", [" ".join(lines)], None)
    return _Item("body", ["\n".join(lines)], _position(element, "spPr"))


def _paragraph_lines(body: Element) -> list[str]:
    lines: list[str] = []
    for paragraph in children(body, "p"):
        text = _paragraph_text(paragraph)
        if not text:
            continue
        props = child(paragraph, "pPr")
        level = int(props.get("lvl", "0")) if props is not None else 0
        bullet = props is not None and (
            child(props, "buChar") is not None or child(props, "buAutoNum") is not None
        )
        if level > 0 or bullet:
            text = "  " * max(level - 1, 0) + "- " + " ".join(text.split())
            lines.append(text)
        else:
            lines.extend(part.strip() for part in text.split("\n") if part.strip())
    return lines


def _paragraph_text(paragraph: Element) -> str:
    """Текст абзаца; верхний индекс (`baseline` > 0) — `<sup>…</sup>`, как у
    .xlsx: иначе номер сноски прилипает к числу."""
    parts: list[str] = []
    for item in paragraph:
        name = local(item.tag)
        if name in ("r", "fld"):
            text = child(item, "t")
            value = text.text or "" if text is not None else ""
            props = child(item, "rPr")
            raised = props is not None and int(props.get("baseline", "0")) > 0
            parts.append(f"<sup>{value}</sup>" if raised and value.strip() else value)
        elif name == "br":
            parts.append("\n")
    return "".join(parts).strip()


# --- Таблица, диаграмма, SmartArt -----------------------------------------------------


def _frame(element: Element, context: _Context) -> list[str]:
    graphic = child(element, "graphic")
    data = child(graphic, "graphicData") if graphic is not None else None
    if data is None:
        return []
    for content in data:
        name = local(content.tag)
        if name == "tbl":
            context.presentation.tables += 1
            return _table(content)
        if name == "chart":
            part = context.targets.get(rel_id(content), "")
            if part in context.names:
                context.presentation.charts += 1
                return _chart(parse_tree(context.archive, part))
        if name == "relIds":
            part = context.targets.get(_dm_id(content), "")
            if part in context.names:
                context.presentation.diagrams += 1
                return _diagram(parse_tree(context.archive, part))
    return []


def _table(table: Element) -> list[str]:
    cells: dict[tuple[int, int], str] = {}
    merges: list[tuple[int, int, int, int]] = []
    for r, row in enumerate(children(table, "tr"), start=1):
        for c, cell in enumerate(children(row, "tc"), start=1):
            if cell.get("hMerge") or cell.get("vMerge"):
                continue  # продолжение объединённой ячейки
            body = child(cell, "txBody")
            text = " ".join(_paragraph_lines(body)) if body is not None else ""
            if text:
                cells[(r, c)] = text
            span_rows = int(cell.get("rowSpan", "1"))
            span_cols = int(cell.get("gridSpan", "1"))
            if span_rows > 1 or span_cols > 1:
                merges.append((r, c, r + span_rows - 1, c + span_cols - 1))
    lines = table_lines(Sheet(name="", cells=cells, merges=merges), base_level=1)
    return ["\n".join(lines)] if lines else []


def _chart(root: Element) -> list[str]:
    """Сохранённые значения диаграммы → таблица категории × ряды."""
    title = " ".join(t.text or "" for t in _find_all(_find(root, "title"), "t")).strip()
    series: list[tuple[str, dict[int, str], dict[int, str]]] = []
    for ser in _find_all(root, "ser"):
        name = " ".join(v.text or "" for v in _find_all(child(ser, "tx"), "v")).strip()
        categories = _points(child(ser, "cat"))
        values = _points(child(ser, "val"))
        if values:
            series.append((name, categories, values))
    if not series:
        return [f"Диаграмма: {title}"] if title else []

    indexes = sorted({i for _, cats, vals in series for i in (*cats, *vals)})
    categories = next((cats for _, cats, _ in series if cats), {})
    header = ["", *(name or f"Ряд {n}" for n, (name, _, _) in enumerate(series, 1))]
    rows = [
        [categories.get(i, str(i + 1)), *(vals.get(i, "") for _, _, vals in series)]
        for i in indexes
    ]
    table = [_pipe(header), _pipe(["---"] * len(header)), *(_pipe(r) for r in rows)]
    caption = f"Диаграмма: {title}" if title else "Диаграмма"
    return [caption, "\n".join(table)]


def _points(holder: Element | None) -> dict[int, str]:
    """c:pt кэша ряда (строки или числа по formatCode) → {idx: текст}."""
    if holder is None:
        return {}
    result: dict[int, str] = {}
    for cache in (*_find_all(holder, "strCache"), *_find_all(holder, "numCache")):
        code_holder = child(cache, "formatCode")
        code = (code_holder.text or "General") if code_holder is not None else "General"
        is_number = local(cache.tag) == "numCache"
        for point in children(cache, "pt"):
            value = child(point, "v")
            if value is None or value.text is None:
                continue
            text = value.text.strip()
            if is_number:
                with suppress(ValueError):
                    text = format_number(float(text), code)
            result[int(point.get("idx", "0"))] = text
    return result


def _diagram(root: Element) -> list[str]:
    """Текст узлов SmartArt списком."""
    lines: list[str] = []
    for point in _find_all(root, "pt"):
        if point.get("type", "node") != "node":
            continue
        body = child(point, "t")
        if body is None:
            continue
        text = " ".join(_paragraph_lines(body)).strip()
        if text:
            lines.append(f"- {text}")
    return ["\n".join(lines)] if lines else []


def _dm_id(element: Element) -> str:
    for key, value in element.attrib.items():
        if key.startswith("{") and local(key) == "dm":
            return value
    return ""


def _find(element: Element | None, name: str) -> Element | None:
    if element is None:
        return None
    for item in element.iter():
        if local(item.tag) == name:
            return item
    return None


def _find_all(element: Element | None, name: str) -> list[Element]:
    if element is None:
        return []
    return [item for item in element.iter() if local(item.tag) == name]


# --- Заметки и Markdown ---------------------------------------------------------------


def _notes(root: Element) -> list[str]:
    tree = _sp_tree(root)
    if tree is None:
        return []
    lines: list[str] = []
    for shape in tree:
        if local(shape.tag) != "sp" or _placeholder(shape) != "body":
            continue
        body = child(shape, "txBody")
        if body is not None:
            lines.extend(_paragraph_lines(body))
    return lines


_SPACES = re.compile(r"[ \t]+")


def _pipe(cells: list[str]) -> str:
    return (
        "| " + " | ".join(_SPACES.sub(" ", c).replace("|", "\\|") for c in cells) + " |"
    )


def presentation_to_markdown(presentation: Presentation) -> str:
    parts: list[str] = []
    for slide in presentation.slides:
        lines = [f"# {' '.join(slide.title.split())}", ""]
        for block in slide.blocks:
            lines.extend([block, ""])
        if slide.notes:
            lines.extend(["Заметки докладчика:", *slide.notes, ""])
        parts.append("\n".join(lines).strip())
    return "\n\n".join(parts).strip() + "\n" if parts else ""
