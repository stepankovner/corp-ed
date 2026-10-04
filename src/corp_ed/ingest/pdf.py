"""PDF → Markdown на pdfplumber / pdfminer.six (MIT) — замена pymupdf4llm (П-12).

pymupdf, pymupdf4llm и модель разметки pymupdf-layout — под AGPL-3.0 или
лицензией Artifex; в закрытом репозитории без лицензии их держать нельзя.
Этот разбор — только пакеты с разрешительной лицензией (pdfplumber,
pdfminer.six — MIT; pypdfium2 — BSD-3 / Apache-2.0). Замер —
`docs/ml-formats.md`, «PDF без AGPL».

Как устроено:
- порядок чтения и абзацы — анализ страницы pdfminer (текстовые блоки,
  колонки); строка, выровненная по ширине, не рвётся на куски
  (`char_margin`);
- таблицы — pdfplumber по линиям сетки, текст ячейки — по символам;
  таблица, разрезанная страницей, получает шапку первой части;
- заголовки — по кеглю (до трёх уровней крупнее основного текста) и по
  жирной отдельной строке с номером или ПРОПИСНЫМИ буквами; строка-
  вводная «2.1. Предприятие обязуется:» перед списком — подзаголовок;
- обложка длинного документа и шапка, которая встречается только в
  начале («Приложение № 1 к Документации»), — не заголовки: иначе их
  текст попадает в крошки каждого фрагмента;
- верхний индекс (номер сноски) — `<sup>`, его снимает preprocess.

Выход — как ждёт preprocess (так же было у pymupdf4llm): заголовки #,
жирные абзацы **…**, таблицы |…| с шапкой и |---|, страницы через \\f.
Один проход по страницам со сбросом кэша страницы: 1 020 страниц — 74 с
и ~95 МБ памяти (замер 04.10).

Чего не умеет (и не обещать клиенту): таблицы без линий сетки читаются
как текст, порядок ячеек может перемешаться; сканы без текстового слоя
(OCR — не MVP).
"""

import io
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import pdfplumber
from pdfminer.layout import LTAnno, LTChar, LTTextBoxHorizontal, LTTextLineHorizontal
from pdfminer.pdfdocument import PDFPasswordIncorrect

from corp_ed.ingest.ooxml import OfficeFileError

PAGE_BREAK = "\f"
"""Разделитель страниц — как у pymupdf4llm (preprocess.PAGE_BREAK)."""
MAX_PAGES = 1000
"""Как MAX_PDF_PAGES в extract.py."""
LAPARAMS = {
    "line_overlap": 0.5,
    "char_margin": 6.0,
    "line_margin": 0.5,
    "word_margin": 0.1,
    "boxes_flow": 0.5,
}
"""char_margin 6 (по умолчанию 2): строка, выровненная по ширине, не рвётся
на куски с широкими пробелами, а две колонки ещё не сливаются."""

MIN_HEADING_DELTA = 1.0
MAX_HEADING_LEVELS = 3
MAX_HEADING_CHARS = 200
MAX_BOLD_HEADING_WORDS = 25
MAX_LEAD_CHARS = 90
SUP_SIZE = 0.85
SUP_RAISE = 0.2
MAX_FURNITURE_LETTERS = 40
MAX_HEADER_CELL_CHARS = 80
MAX_COVER_BODY_LETTERS = 200

_BOLD_FONT = re.compile(r"bold|black|heavy|semibold|demi", re.IGNORECASE)
_LIST_START = re.compile(r"^(?:\d+(?:\.\d+)*[.)]?|[а-яa-z]\)|[-–—•●▪■◦*])\s")
_NUMBERING = re.compile(r"^(\d{1,2}(?:\.\d{1,3})*)[.)]?(?:\s|$)")
"""Номер раздела: «3», «2.1», «4.9.1)» — но не год «2025 год»."""
_SECTION_WORD = re.compile(
    r"^(?:[IVXLC]+[.)]\s|(?:раздел|глава|статья|приложение)\b)", re.IGNORECASE
)
_PLACE_AND_YEAR = re.compile(
    r"^(?:г\.\s*)?[А-ЯЁ][А-Яа-яЁё.\- ]{1,30},?\s+(?:[–—-]\s*)?\d{4}\s*"
    r"(?:г\.|год|года)?(?:\s*\d{1,4})?$"
)
"""Строка обложки «г. Москва 2025 год» (бывает с номером страницы в той же
строке) — не заголовок."""
_SENTENCE_END = re.compile(r"[.!?:;»\")]\s*(?:<sup>[^<]*</sup>)?$")
_LEAD_IN_END = re.compile(r":\s*(?:<sup>[^<]*</sup>)?$")
_DATA_START = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?|[IVXLC]+\.)\s*$|^\s*\d")
"""Первая ячейка строки данных: номер «11.16», «1.», «IV.»."""


class PdfError(OfficeFileError):
    """PDF нельзя принять; code — коды `ExtractionError`: format_mismatch,
    encrypted, corrupted, too_many_pages."""


@dataclass
class _Line:
    text: str
    size: float
    bold: bool
    letters: int


@dataclass
class _Block:
    top: float
    x0: float = 0.0
    x1: float = 0.0
    lines: list[_Line] = field(default_factory=list)
    rows: list[list[str]] | None = None
    """Строки таблицы (ячейки); None — текстовый блок."""
    table_sizes: Counter[float] = field(default_factory=Counter)
    """Кегль букв ячеек — в шрифтовой профиль документа: в документе-
    таблице иначе «основным» окажется кегль заголовка."""


@dataclass(frozen=True)
class _Glyph:
    """Символ в общем виде: up — нижняя граница (растёт вверх); space —
    пробел, который вставил pdfminer или разрыв между словами."""

    text: str
    size: float
    font: str
    x0: float
    x1: float
    up: float
    space: bool = False


_SPACE = _Glyph(" ", 0.0, "", 0.0, 0.0, 0.0, space=True)


def check_container(data: bytes) -> None:
    """Сигнатура, пароль, число страниц — до разбора (как у xlsx и doc)."""
    with _open(data) as pdf:
        if len(pdf.pages) > MAX_PAGES:
            raise PdfError("too_many_pages")


def pdf_to_markdown(data: bytes) -> str:
    """PDF → Markdown до preprocess. Вызывать в песочнице."""
    pages: list[list[_Block]] = []
    sizes: Counter[float] = Counter()
    with _open(data) as pdf:
        if len(pdf.pages) > MAX_PAGES:
            raise PdfError("too_many_pages")
        try:
            for page in pdf.pages:
                blocks = _page_blocks(page)
                for block in blocks:
                    sizes.update(block.table_sizes)
                    for line in block.lines:
                        sizes[line.size] += line.letters
                pages.append(blocks)
                page.flush_cache()
        except PdfError:
            raise
        except Exception as exc:  # noqa: BLE001 — любая ошибка разбора = битый файл
            raise PdfError("corrupted") from exc
    _continue_tables(pages)
    body, levels = _levels(sizes)
    # Шапку и обложку ищем только у длинных документов: у одностраничной
    # памятки крупная строка — её название, в крошках оно полезно.
    if len(pages) >= 3:
        levels = _drop_preamble_levels(pages, levels)
    cover = bool(pages) and _is_cover(pages[0], body, len(pages))
    return PAGE_BREAK.join(
        _render(blocks, levels, cover=cover and number == 0)
        for number, blocks in enumerate(pages)
    )


def _open(data: bytes) -> Any:
    if not data.startswith(b"%PDF-"):
        raise PdfError("format_mismatch")
    try:
        return pdfplumber.open(io.BytesIO(data), laparams=LAPARAMS)
    except Exception as exc:  # noqa: BLE001 — любая ошибка парсера = битый файл
        # pdfplumber заворачивает ошибку pdfminer: PdfminerException(причина).
        if any(isinstance(item, PDFPasswordIncorrect) for item in (exc, *exc.args)):
            raise PdfError("encrypted") from exc
        raise PdfError("corrupted") from exc


# --- символы → текст ---------------------------------------------------------


def _round(size: float) -> float:
    return round(float(size), 1)


def _from_layout(items: Iterable[Any]) -> list[_Glyph]:
    glyphs: list[_Glyph] = []
    for item in items:
        if isinstance(item, LTAnno):
            if item.get_text() == " ":
                glyphs.append(_SPACE)
        elif isinstance(item, LTChar):
            glyphs.append(
                _Glyph(
                    item.get_text(),
                    float(item.size),
                    item.fontname,
                    item.x0,
                    item.x1,
                    item.y0,
                )
            )
    return glyphs


def _from_plumber(chars: Sequence[dict[str, Any]]) -> list[_Glyph]:
    """Символы pdfplumber одной строки (слева направо); пробел — по разрыву."""
    glyphs: list[_Glyph] = []
    prev: dict[str, Any] | None = None
    for char in chars:
        if prev is not None and char["x0"] - prev["x1"] > 0.25 * float(char["size"]):
            glyphs.append(_SPACE)
        glyphs.append(
            _Glyph(
                char["text"],
                float(char["size"]),
                char["fontname"],
                char["x0"],
                char["x1"],
                -float(char["bottom"]),
            )
        )
        prev = char
    return glyphs


def _glyphs_text(glyphs: list[_Glyph]) -> tuple[str, float, bool, int]:
    """Строка символов → текст с <sup>; кегль строки, жирность, число букв."""
    visible = [g for g in glyphs if not g.space and g.text.strip()]
    if not visible:
        return "", 0.0, False, 0
    sizes = Counter(_round(g.size) for g in visible if g.text.isalpha())
    main = (sizes or Counter(_round(g.size) for g in visible)).most_common(1)[0][0]
    base = min(g.up for g in visible if _round(g.size) == main)

    def raised(glyph: _Glyph) -> bool:
        return glyph.size < SUP_SIZE * main and glyph.up > base + SUP_RAISE * main

    def phantom(i: int) -> bool:
        """Пробел без промежутка между буквами — остаток переноса строки в
        PDF из Word («примене|нием»): буквы вокруг него стоят вплотную."""
        before = next(
            (g for g in reversed(glyphs[:i]) if not g.space and g.text.strip()), None
        )
        after = next(
            (g for g in glyphs[i + 1 :] if not g.space and g.text.strip()), None
        )
        return (
            before is not None
            and after is not None
            and after.x0 - before.x1 < 0.15 * main
        )

    out: list[str] = []
    in_sup = False
    for i, glyph in enumerate(glyphs):
        blank = glyph.space or not glyph.text.strip()
        if blank and not glyph.space and phantom(i):
            continue
        if blank:
            if in_sup:
                out.append("</sup>")
                in_sup = False
            out.append(" ")
            continue
        up = raised(glyph)
        if up != in_sup:
            out.append("<sup>" if up else "</sup>")
            in_sup = up
        out.append(glyph.text)
    if in_sup:
        out.append("</sup>")
    text = " ".join("".join(out).split())
    letters = [g for g in visible if g.text.isalpha() and not raised(g)]
    bold = bool(letters) and (
        sum(bool(_BOLD_FONT.search(g.font)) for g in letters) >= 0.8 * len(letters)
    )
    return text, main, bold, len(letters)


def _join(prev: str, nxt: str) -> str:
    """Склейка строк абзаца или ячейки: мягкий перенос — без дефиса; дефис в
    конце строки — составное слово или код («научно-технической»,
    «С1ИИ-602444»); иначе пробел."""
    if prev.endswith("­"):
        return prev[:-1] + nxt
    if (
        prev.endswith("-")
        and len(prev) > 1
        and prev[-2].isalnum()
        and (nxt[:1].islower() or nxt[:1].isdigit())
    ):
        return prev + nxt
    return f"{prev} {nxt}"


# --- страница → блоки ------------------------------------------------------


def _page_blocks(page: Any) -> list[_Block]:
    tables = page.find_tables()
    boxes = [table.bbox for table in tables]
    height = float(page.height)
    blocks: list[_Block] = []
    for element in page.layout:
        if not isinstance(element, LTTextBoxHorizontal):
            continue
        # Блок целиком не выбрасывается: pdfminer иногда кладёт в один блок
        # заголовок и строки таблицы под ним — отсеиваются строки.
        block = _Block(top=height - element.y1, x0=element.x0, x1=element.x1)
        for line in element:
            if not isinstance(line, LTTextLineHorizontal):
                continue
            if boxes and _inside(
                (line.x0, height - line.y1, line.x1, height - line.y0), boxes
            ):
                continue
            text, size, bold, letters = _glyphs_text(_from_layout(line))
            if text:
                block.lines.append(_Line(text, size, bold, letters))
        if block.lines:
            blocks.append(block)
    for table in tables:
        table_sizes: Counter[float] = Counter()
        rows = _table_rows(page, table, table_sizes)
        if rows:
            _insert_table(blocks, table.bbox, rows, table_sizes)
    return blocks


def _inside(bbox: tuple[float, float, float, float], boxes: Sequence[Any]) -> bool:
    x0, top, x1, bottom = bbox
    cx, cy = (x0 + x1) / 2, (top + bottom) / 2
    return any(bool(b[0] <= cx <= b[2] and b[1] <= cy <= b[3]) for b in boxes)


def _insert_table(
    blocks: list[_Block], bbox: Any, rows: list[list[str]], sizes: Counter[float]
) -> None:
    """Таблица — среди блоков своей колонки: перед первым блоком ниже неё,
    иначе после последнего блока колонки, иначе в конец страницы."""
    tx0, ttop, tx1 = float(bbox[0]), float(bbox[1]), float(bbox[2])

    def same_column(block: _Block) -> bool:
        width = min(block.x1 - block.x0, tx1 - tx0)
        return width > 0 and min(block.x1, tx1) - max(block.x0, tx0) > 0.5 * width

    column = [i for i, b in enumerate(blocks) if b.rows is None and same_column(b)]
    below = [i for i in column if blocks[i].top >= ttop]
    position = below[0] if below else (column[-1] + 1 if column else len(blocks))
    table = _Block(top=ttop, x0=tx0, x1=tx1, rows=rows, table_sizes=sizes)
    blocks.insert(position, table)


def _table_rows(page: Any, table: Any, sizes: Counter[float]) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in table.rows:
        cells = [_cell_text(page, cell, sizes) if cell else "" for cell in row.cells]
        if any(cells):
            rows.append(cells)
    return rows


def _cell_text(page: Any, bbox: Any, sizes: Counter[float]) -> str:
    chars: list[dict[str, Any]] = page.crop(bbox, strict=False).chars
    for char in chars:
        if char["text"].isalpha():
            sizes[_round(char["size"])] += 1
    lines: list[list[dict[str, Any]]] = []
    for char in sorted(chars, key=lambda c: (round(c["top"]), c["x0"])):
        for line in lines:
            ref = line[0]
            overlap = min(ref["bottom"], char["bottom"]) - max(ref["top"], char["top"])
            if overlap > 0.3 * min(
                ref["bottom"] - ref["top"], char["bottom"] - char["top"]
            ):
                line.append(char)
                break
        else:
            lines.append([char])
    texts = []
    for line in sorted(lines, key=lambda chars_: min(c["top"] for c in chars_)):
        text, *_ = _glyphs_text(_from_plumber(sorted(line, key=lambda c: c["x0"])))
        if text:
            texts.append(text)
    if not texts:
        return ""
    joined = texts[0]
    for text in texts[1:]:
        joined = _join(joined, text)
    return joined.replace("|", "\\|")


# --- документ: таблицы, уровни, обложка ---------------------------------------


def _continue_tables(pages: list[list[_Block]]) -> None:
    """Таблица, разрезанная страницей, получает шапку первой части.

    Продолжение — таблица того же числа столбцов сразу за предыдущей (между
    ними только номер страницы или колонтитул), первая строка которой не
    похожа на шапку (номер «11.16», длинные ячейки). Шапка ставится копией,
    а не наследуется в preprocess: между частями бывают сноски внизу
    страницы, и наследование там обрывается. Повтор шапки («повторять как
    заголовок» в Word) остаётся как есть.
    """
    head: _Block | None = None
    gap = 0
    for blocks in pages:
        for block in blocks:
            if not block.rows:
                gap += sum(line.letters for line in block.lines)
                if gap > MAX_FURNITURE_LETTERS:
                    head = None
                continue
            continued = False
            if head is not None and head.rows and _width(block) == _width(head):
                if _same_row(block.rows[0], head.rows[0]):
                    continued = True
                elif not _looks_like_header(block.rows[0]):
                    block.rows = [list(head.rows[0]), *block.rows]
                    continued = True
            if not continued:
                head = block
            gap = 0


def _width(block: _Block) -> int:
    return max((len(row) for row in block.rows or []), default=0)


def _same_row(a: list[str], b: list[str]) -> bool:
    return [" ".join(x.split()) for x in a] == [" ".join(x.split()) for x in b]


def _looks_like_header(row: list[str]) -> bool:
    """Шапка таблицы: ячейки короткие, первая не номер строки данных."""
    values = [" ".join(cell.split()) for cell in row if cell.strip()]
    if len(values) < 2 or any(len(v) > MAX_HEADER_CELL_CHARS for v in values):
        return False
    return not (row[0].strip() and _DATA_START.match(row[0]))


def _levels(sizes: Counter[float]) -> tuple[float, list[float]]:
    """Основной кегль — самый частый по числу букв; уровни заголовков —
    кегли крупнее него, у которых букв не совсем мало (не буквица)."""
    if not sizes:
        return 0.0, []
    body = sizes.most_common(1)[0][0]
    total = sum(sizes.values())
    larger = [
        size
        for size in sizes
        if size >= body + MIN_HEADING_DELTA and sizes[size] >= max(5, total * 0.0005)
    ]
    return body, sorted(larger, reverse=True)[:MAX_HEADING_LEVELS]


def _drop_preamble_levels(
    pages: list[list[_Block]], levels: list[float]
) -> list[float]:
    """Верхний уровень, который встречается только в начале, до первого
    заголовка другого уровня, — шапка («Приложение № 1 к Документации»,
    «Приоритетные направления…»), а не раздел: иначе её длинный текст
    попадает в крошки каждого фрагмента. Такие строки идут жирным текстом.
    """
    sequence: list[int] = []
    for blocks in pages:
        for block in blocks:
            for line in block.lines:
                if (
                    line.size in levels
                    and line.letters >= 2
                    and not _PLACE_AND_YEAR.match(line.text)
                ):
                    sequence.append(levels.index(line.size))
                elif (
                    line.bold
                    and len(line.text) <= MAX_HEADING_CHARS
                    and _looks_like_section(line.text)
                ):
                    sequence.append(len(levels))
    while levels and sequence:
        first_other = next((i for i, level in enumerate(sequence) if level != 0), None)
        if first_other is None or 0 in sequence[first_other:]:
            break
        levels = levels[1:]
        sequence = [level - 1 for level in sequence if level != 0]
    return levels


def _is_cover(blocks: list[_Block], body: float, page_count: int) -> bool:
    """Обложка: первая страница документа от трёх страниц, без таблиц и почти
    без текста основного кегля (у «Победителей» на ней уже таблица)."""
    if page_count < 3 or any(block.rows for block in blocks):
        return False
    body_letters = sum(
        line.letters for block in blocks for line in block.lines if line.size == body
    )
    return body_letters < MAX_COVER_BODY_LETTERS


def _looks_like_section(title: str) -> bool:
    """Номер («1.», «2.3», «1)», «IV.», «Раздел 2») или ПРОПИСНЫЕ буквы:
    жирные строки обложки («Москва 2026 г.») заголовками не становятся."""
    if _NUMBERING.match(title) or _SECTION_WORD.match(title):
        return True
    letters = [ch for ch in title if ch.isalpha()]
    return len(letters) >= 3 and sum(ch.isupper() for ch in letters) >= 0.7 * len(
        letters
    )


# --- блоки → Markdown --------------------------------------------------------


def _render(blocks: list[_Block], levels: list[float], *, cover: bool) -> str:
    """Страница → Markdown. cover — обложка: крупные строки без номера идут
    жирным текстом, а не заголовками."""
    out: list[str] = []
    plain_last = False  # последний элемент out — обычный абзац
    bold_level = len(levels) + 1
    for block in blocks:
        if block.rows is not None:
            if block.rows:
                out.append(_rows_markdown(block.rows))
            plain_last = False
            continue
        para: list[str] = []
        para_bold = True
        heading: tuple[int, list[str]] | None = None
        first = block.lines[0]
        # Абзац, перешедший в следующую колонку или блок: у предыдущего нет
        # точки в конце, этот начинается со строчной буквы или числа.
        continues = first.text[:1].islower() or (
            first.text[:1].isdigit() and not _LIST_START.match(first.text)
        )
        if (
            plain_last
            and not _SENTENCE_END.search(out[-1])
            and continues
            and not first.bold
        ):
            para, para_bold = [out.pop()], False

        def flush_para() -> None:
            nonlocal para, para_bold, plain_last
            if para:
                text = para[0]
                for nxt in para[1:]:
                    text = _join(text, nxt)
                title = " ".join(text.split())
                plain_last = False
                if (
                    para_bold
                    and len(title) <= MAX_HEADING_CHARS
                    and len(title.split()) <= MAX_BOLD_HEADING_WORDS
                    and _looks_like_section(title)
                    and not _PLACE_AND_YEAR.match(title)
                ):
                    out.append("#" * min(bold_level, 6) + " " + title)
                elif para_bold:
                    out.append(f"**{text}**")
                else:
                    out.append(text)
                    plain_last = True
            para, para_bold = [], True

        def flush_heading() -> None:
            nonlocal heading, plain_last
            if heading:
                title = " ".join(heading[1])
                # «г. Москва» + «2026 год» — две строки одной обложки.
                is_cover_line = _PLACE_AND_YEAR.match(title) is not None
                out.append(title if is_cover_line else "#" * heading[0] + " " + title)
                plain_last = False
            heading = None

        for line in block.lines:
            numbered = bool(
                _NUMBERING.match(line.text) or _SECTION_WORD.match(line.text)
            )
            is_heading = (
                line.size in levels
                and (not cover or numbered)
                and len(line.text) <= MAX_HEADING_CHARS
                and line.letters >= 2
                and not _PLACE_AND_YEAR.match(line.text)
            )
            if is_heading:
                flush_para()
                level = levels.index(line.size) + 1
                if heading and heading[0] == level:
                    heading[1].append(line.text)
                else:
                    flush_heading()
                    heading = (level, [line.text])
                continue
            flush_heading()
            if para and (_LIST_START.match(line.text) or line.bold != para_bold):
                flush_para()
            para.append(line.text)
            para_bold = para_bold and line.bold
        flush_heading()
        flush_para()
    _promote_lead_ins(out, bold_level)
    return "\n\n".join(out)


def _rows_markdown(rows: list[list[str]]) -> str:
    width = max(len(row) for row in rows)
    lines = []
    for i, cells in enumerate(rows):
        lines.append("|" + "|".join(cells + [""] * (width - len(cells))) + "|")
        if i == 0:
            lines.append("|" + "|".join(["---"] * width) + "|")
    return "\n".join(lines)


def _promote_lead_ins(out: list[str], bold_level: int) -> None:
    """«2.1. Предприятие обязуется:» перед списком или таблицей — подзаголовок.

    Так оформлены пункты договоров и положений: шрифт обычный, но это
    раздел, и без него у фрагментов «Фонд обязуется» и «Предприятие
    обязуется» одинаковые крошки. Уровень — по глубине номера.
    """
    for i, item in enumerate(out[:-1]):
        if item[:1] in "#|*" or "\n" in item or len(item) > MAX_LEAD_CHARS:
            continue
        numbering = _NUMBERING.match(item)
        if numbering is None or not _LEAD_IN_END.search(item):
            continue
        following = out[i + 1]
        if following.startswith("|") or _LIST_START.match(following):
            depth = numbering.group(1).count(".") + 1
            out[i] = "#" * min(bold_level + depth - 1, 6) + " " + item
