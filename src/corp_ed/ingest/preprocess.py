"""Предобработка Markdown перед нарезкой (задача A2).

Вход — Markdown, который бэкенд получил из docx/pdf/txt/md. Выход — тот же
Markdown, но без мусора, который ест токены и ломает поиск:

1. Символы: NFC, неразрывные и «тонкие» пробелы, мягкие переносы, \\r\\n.
   PDF часто отдаёт «й» и «ё» двумя кодовыми точками — без NFC одно и то
   же слово в вопросе и в документе не совпадает побайтно.
2. Колонтитулы PDF. Если бэкенд разделил страницы символом \\f (см.
   PAGE_BREAK), строки, которые повторяются у края большинства страниц,
   удаляются. Отдельно, для любых документов, удаляются строки из одного
   номера страницы («4», «- 4 -», «стр. 4 из 85»). Правило проверено на
   реальных PDF: номер страницы стоит на 83 из 85 страниц, а, например,
   «Приложение № N к Договору» — лишь на 10 из 85, и это настоящий
   заголовок, его трогать нельзя. Отсюда порог «больше половины страниц».
3. Встроенный HTML от pymupdf4llm: <br> в ячейках, <u>, <sup>13</sup>
   (номера сносок) в заголовках.
4. Таблицы → строки «Заголовок1: значение; Заголовок2: значение». Чанк,
   разрезавший таблицу, без шапки бесполезен; строка с ключами
   самодостаточна.
5. Ссылки: [текст](url) → текст, голые URL удаляются. Почтовые адреса
   остаются — это содержательный ответ на «куда писать».
6. Экранирование markdownify (tenant\\_id → tenant_id).
7. Заголовки #–###### сохраняются, из их текста убирается разметка
   (# **ПОЛОЖЕНИЕ** → # ПОЛОЖЕНИЕ): на заголовках держится нарезка.
8. Если в документе нет ни одного заголовка #, полностью жирные короткие
   строки становятся заголовками: docx без стилей и многие PDF оформляют
   разделы жирным абзацем. Уровень берётся из нумерации («3.2» → ###).
9. Оглавление («Содержание», «Оглавление») удаляется целиком, точки-
   заполнители «........ 12» — везде. Оглавление содержит названия всех
   разделов и слабо похоже на любой вопрос: такой чанк вытесняет из
   выдачи настоящие ответы.
10. Пробелы: хвостовые убираются, серии пробелов схлопываются, больше
    одной пустой строки подряд не бывает.

Функция чистая: ни файлов, ни сети. На обычных документах повторный вызов
на своём же выходе ничего не меняет (проверяется тестом).
"""

import re
import unicodedata
from collections import Counter

from corp_ed.domain.markdown import (
    ATX_HEADING,
    clean_heading_text,
    fenced_lines,
    strip_emphasis,
)

PAGE_BREAK = "\f"
"""Разделитель страниц PDF, который ставит бэкенд при извлечении.

Договорённость с бэкендом: pymupdf4llm.to_markdown(..., page_chunks=True)
и склейка страниц через "\\f". Без разделителя колонтитулы по частоте не
ищутся — остаётся только удаление голых номеров страниц.
"""

_EDGE_LINES = 2
_MIN_PAGES_FOR_FURNITURE = 3

# Неразрывный, «тонкие» и широкие пробелы, табуляция → обычный пробел.
_SPACE_LIKE = (
    "\u00a0\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
    "\u2007\u2008\u2009\u200a\u202f\u205f\u3000\t"
)
# Мягкий перенос, пробелы нулевой ширины, BOM → удаляются.
_INVISIBLE = "\u00ad\u200b\u200c\u200d\u2060\ufeff"
_CHAR_TABLE = {
    **dict.fromkeys(map(ord, _SPACE_LIKE), " "),
    **dict.fromkeys(map(ord, _INVISIBLE), None),
}

_PAGE_NUMBER_LINE = re.compile(
    r"^\s*(?:(?:стр\.?|страница|page)\s*)?[-–—]?\s*\d{1,4}\s*[-–—]?"
    r"\s*(?:(?:из|of|/)\s*\d{1,4})?\s*$",
    re.IGNORECASE,
)

_FOOTNOTE_SUP = re.compile(r"<sup>[\s\d*,]*</sup>", re.IGNORECASE)
_BR_TAG = re.compile(r"<br\s*/?>", re.IGNORECASE)
_INLINE_TAG = re.compile(
    r"</?(?:u|b|i|em|strong|sup|sub|span|s|strike|del|ins|mark|small|font)\b[^>]*>",
    re.IGNORECASE,
)

_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$")
_CELL_BORDER = re.compile(r"(?<!\\)\|")
_PLACEHOLDER_HEADER = re.compile(
    r"^(?:col\s*\d+|column\s*\d+|колонка\s*\d+)$", re.IGNORECASE
)

_IMAGE = re.compile(r"!\[([^\]]*)\]\((?:[^()]|\([^)]*\))*\)")
_INLINE_LINK = re.compile(r"\[([^\]]+)\]\((?:[^()]|\([^)]*\))*\)")
_REFERENCE_LINK = re.compile(r"\[([^\]]+)\]\[[^\]]*\]")
_REFERENCE_DEFINITION = re.compile(r"^ {0,3}\[[^\]]+\]:\s*\S+.*$", re.MULTILINE)
_AUTOLINK = re.compile(
    r"<(?:(?:https?|ftp)://[^>\s]+|mailto:([^>\s]+))>", re.IGNORECASE
)
_BARE_URL = re.compile(
    r"(?:https?://|ftp://|www\.)[^\s<>()\[\]\"«»]*[^\s<>()\[\]\"«».,;:!?'»]",
    re.IGNORECASE,
)
_EMPTY_BRACKETS = re.compile(r"\(\s*\)|\[\s*\]")
_SPACE_BEFORE_PUNCT = re.compile(r"[ ]+([,.;:!?])")

_ESCAPED = re.compile(r"\\([\\`*_{}\[\]()+\-.!|>~])")

_BOLD_LINE = re.compile(r"^\s*(?:\*\*|__)(.+?)(?:\*\*|__)\s*$")
_NUMBERING = re.compile(r"^(\d+(?:\.\d+)*)\.?(?:\s|$)")
_TOC_TITLE = re.compile(
    r"^(?:содержание|оглавление|table of contents|contents)$", re.IGNORECASE
)
_DOT_LEADER = re.compile(r"\s*(?:\.{4,}|…{2,}|(?:\. ){4,})\s*\d*\s*$")

_MAX_PROMOTED_HEADING_CHARS = 100
_MAX_PROMOTED_HEADING_WORDS = 15


def preprocess(markdown: str) -> str:
    text = _normalize_characters(markdown)
    text = _remove_page_furniture(text)
    text = _strip_inline_html(text)
    text = _linearize_tables(text)
    text = _remove_links(text)
    text = _unescape_markdown(text)
    text = _clean_headings(text)
    text = _promote_bold_headings(text)
    text = _remove_table_of_contents(text)
    return _normalize_whitespace(text)


# --- 1. Символы -------------------------------------------------------------


def _normalize_characters(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.translate(_CHAR_TABLE)


# --- 2. Колонтитулы ---------------------------------------------------------


def _remove_page_furniture(text: str) -> str:
    pages = text.split(PAGE_BREAK)
    # Частоту считаем только по страницам, где есть «середина»: на короткой
    # странице края покрывают весь текст, и после маскировки цифр абзацы
    # вроде «Пункт 1.» / «Пункт 2.» выглядели бы одинаковым колонтитулом.
    analyzable = [page for page in pages if _filled_count(page) > 2 * _EDGE_LINES]
    if len(analyzable) >= _MIN_PAGES_FOR_FURNITURE:
        repeated = _repeated_edge_lines(analyzable)
        pages = [_drop_edge_lines(page, repeated) for page in pages]
    text = "\n\n".join(pages)
    return "\n".join(
        line for line in text.split("\n") if not _PAGE_NUMBER_LINE.match(line)
    )


def _filled_count(page: str) -> int:
    return sum(1 for line in page.split("\n") if line.strip())


def _edge_keys(lines: list[str]) -> dict[int, set[str]]:
    """Ключи сравнения для строк у краёв страницы.

    Дословно (регистр и пробелы не важны) сравниваются _EDGE_LINES строк
    сверху и снизу — так ловится постоянная шапка в одну-две строки.
    С маской цифр — только самая крайняя строка: там живут номера страниц
    («лист 4», «стр. 4 из 85»). Маскировать цифры глубже нельзя: строки
    «Статья 5.» и «Статья 6.» в начале страниц стали бы «колонтитулом».
    Заголовки # колонтитулом не бывают: pymupdf4llm размечает ими крупный
    шрифт, а колонтитулы набирают мелким.
    """
    filled = [i for i, line in enumerate(lines) if line.strip()]
    outermost = set(filled[:1] + filled[-1:])
    keys: dict[int, set[str]] = {}

    for i in set(filled[:_EDGE_LINES] + filled[-_EDGE_LINES:]):
        if ATX_HEADING.match(lines[i]):
            continue
        exact = " ".join(lines[i].split()).lower()
        keys[i] = {f"exact:{exact}"}
        if i in outermost:
            keys[i].add("masked:" + re.sub(r"\d+", "#", exact))

    return keys


def _repeated_edge_lines(pages: list[str]) -> set[str]:
    counts: Counter[str] = Counter()
    for page in pages:
        page_keys: set[str] = set()
        for keys in _edge_keys(page.split("\n")).values():
            page_keys |= keys
        counts.update(page_keys)

    majority = len(pages) // 2 + 1
    return {key for key, count in counts.items() if count >= majority}


def _drop_edge_lines(page: str, repeated: set[str]) -> str:
    lines = page.split("\n")
    drop = {i for i, keys in _edge_keys(lines).items() if keys & repeated}
    return "\n".join(line for i, line in enumerate(lines) if i not in drop)


# --- 3. Встроенный HTML -----------------------------------------------------


def _strip_inline_html(text: str) -> str:
    text = _FOOTNOTE_SUP.sub("", text)
    text = _BR_TAG.sub(" ", text)
    return _INLINE_TAG.sub("", text)


# --- 4. Таблицы -------------------------------------------------------------


def _linearize_tables(text: str) -> str:
    """Таблицы GFM → строки «ключ: значение».

    Таблица, продолжающаяся на следующей странице PDF, приходит от
    pymupdf4llm строками |…|…| без шапки и без разделителя |---|. Если
    такие строки идут сразу за таблицей (между ними только пустые строки)
    и число столбцов совпадает, им достаётся шапка предыдущей таблицы —
    иначе продолжение без ключей бесполезно (урок МТС). Если не совпадает —
    линеаризуются просто значения через «; ».
    """
    lines = text.split("\n")
    fenced = fenced_lines(lines)
    result: list[str] = []
    previous_keys: list[str] | None = None
    i = 0

    while i < len(lines):
        line = lines[i]

        if fenced[i]:
            previous_keys = None
            result.append(line)
            i += 1
            continue

        if _starts_table(lines, fenced, i):
            keys = _table_keys(_split_cells(line))
            rows, i = _collect_rows(lines, fenced, i + 2)
            result.extend(["", *_table_to_lines(keys, rows), ""])
            previous_keys = keys
            continue

        if _is_pipe_row(line):
            rows, i = _collect_rows(lines, fenced, i)
            continues_previous = previous_keys is not None and all(
                len(row) == len(previous_keys) for row in rows
            )
            row_keys = previous_keys if continues_previous else None
            result.extend(["", *_rows_to_lines(row_keys, rows), ""])
            continue

        if line.strip():
            previous_keys = None
        result.append(line)
        i += 1

    return "\n".join(result)


def _starts_table(lines: list[str], fenced: list[bool], i: int) -> bool:
    return (
        i + 1 < len(lines)
        and not fenced[i + 1]
        and _CELL_BORDER.search(lines[i]) is not None
        and _TABLE_SEPARATOR.match(lines[i + 1]) is not None
    )


def _is_pipe_row(line: str) -> bool:
    stripped = line.strip()
    return len(stripped) > 1 and stripped.startswith("|") and stripped.endswith("|")


def _collect_rows(
    lines: list[str], fenced: list[bool], i: int
) -> tuple[list[list[str]], int]:
    rows: list[list[str]] = []
    while (
        i < len(lines)
        and not fenced[i]
        and lines[i].strip()
        and _CELL_BORDER.search(lines[i]) is not None
    ):
        if not _TABLE_SEPARATOR.match(lines[i]):
            rows.append(_split_cells(lines[i]))
        i += 1
    return rows, i


def _split_cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [cell.replace("\\|", "|") for cell in _CELL_BORDER.split(row)]


def _clean_cell(cell: str) -> str:
    return " ".join(strip_emphasis(cell).split())


def _table_keys(header: list[str]) -> list[str]:
    keys = [_clean_cell(cell).rstrip(":").strip() for cell in header]
    return ["" if _PLACEHOLDER_HEADER.match(key) else key for key in keys]


def _rows_to_lines(keys: list[str] | None, rows: list[list[str]]) -> list[str]:
    """Каждая строка таблицы → «ключ: значение; …» (без ключей — значения)."""
    lines: list[str] = []
    for row in rows:
        pairs: list[str] = []
        for index, raw in enumerate(row):
            value = _clean_cell(raw)
            if not value:
                continue
            key = keys[index] if keys is not None and index < len(keys) else ""
            pairs.append(f"{key}: {value}" if key and key != value else value)
        if pairs:
            lines.append("; ".join(pairs))
    return lines


def _table_to_lines(keys: list[str], rows: list[list[str]]) -> list[str]:
    if len(keys) == 1:
        # Одна колонка: «ключ: значение» бессмысленно, шапка — такая же
        # строка текста, как остальные (так pymupdf4llm рисует оглавления).
        head = [keys[0]] if keys[0] else []
        return head + _rows_to_lines(None, rows)

    lines = _rows_to_lines(keys, rows)
    if not lines:
        # Таблица из одной строки-шапки: pymupdf4llm так оформляет
        # отдельные строки анкет. Текст терять нельзя.
        header_line = "; ".join(key for key in keys if key)
        return [header_line] if header_line else []

    return lines


# --- 5. Ссылки ---------------------------------------------------------------


def _remove_links(text: str) -> str:
    text = _IMAGE.sub(r"\1", text)
    text = _INLINE_LINK.sub(r"\1", text)
    text = _REFERENCE_LINK.sub(r"\1", text)
    text = _REFERENCE_DEFINITION.sub("", text)
    text = _AUTOLINK.sub(lambda m: m.group(1) or "", text)
    return "\n".join(_remove_bare_urls(line) for line in text.split("\n"))


def _remove_bare_urls(line: str) -> str:
    cleaned = _BARE_URL.sub("", line)
    if cleaned == line:
        return line
    cleaned = _EMPTY_BRACKETS.sub("", cleaned)
    return _SPACE_BEFORE_PUNCT.sub(r"\1", cleaned)


# --- 6. Экранирование --------------------------------------------------------


def _unescape_markdown(text: str) -> str:
    return _ESCAPED.sub(r"\1", text)


# --- 7–9. Заголовки и оглавление --------------------------------------------


def _clean_headings(text: str) -> str:
    lines = text.split("\n")
    fenced = fenced_lines(lines)
    result: list[str] = []

    for line, in_code in zip(lines, fenced, strict=True):
        match = None if in_code else ATX_HEADING.match(line)
        if match is None:
            result.append(line)
            continue
        title = clean_heading_text(match.group(2))
        if title:
            result.append(f"{match.group(1)} {title}")

    return "\n".join(result)


def _has_headings(lines: list[str], fenced: list[bool]) -> bool:
    return any(
        ATX_HEADING.match(line) and not in_code
        for line, in_code in zip(lines, fenced, strict=True)
    )


def _heading_level(title: str) -> int:
    numbering = _NUMBERING.match(title)
    if numbering is None:
        return 2
    depth = numbering.group(1).count(".") + 1
    return min(1 + depth, 6)


def _promote_bold_headings(text: str) -> str:
    lines = text.split("\n")
    fenced = fenced_lines(lines)
    if _has_headings(lines, fenced):
        return text

    result: list[str] = []
    for line, in_code in zip(lines, fenced, strict=True):
        match = None if in_code else _BOLD_LINE.match(line)
        title = " ".join(strip_emphasis(match.group(1)).split()) if match else ""
        looks_like_heading = (
            bool(title)
            and len(title) <= _MAX_PROMOTED_HEADING_CHARS
            and len(title.split()) <= _MAX_PROMOTED_HEADING_WORDS
            and any(char.isalpha() for char in title)
        )
        if looks_like_heading:
            result.append(f"{'#' * _heading_level(title)} {title}")
        else:
            result.append(line)

    return "\n".join(result)


def _remove_table_of_contents(text: str) -> str:
    """Удалить оглавление: заголовок «Содержание» и тело до следующего заголовка.

    Граница — следующий заголовок ЛЮБОГО уровня, а не «того же или выше»:
    pymupdf4llm назначает уровни по размеру шрифта, и в реальном PDF
    «# СОДЕРЖАНИЕ» стоял уровнем выше всех разделов «### …». Правило
    «до заголовка того же уровня» стёрло бы весь документ (так и было
    в первой версии — поймано на УМНИК-2026). По той же причине тело
    удаляется, только если оно похоже на оглавление: большинство строк
    кончаются номером страницы или точками-заполнителями.
    """
    lines = text.split("\n")
    fenced = fenced_lines(lines)
    headings = [
        i
        for i, (line, in_code) in enumerate(zip(lines, fenced, strict=True))
        if not in_code and ATX_HEADING.match(line)
    ]
    drop: set[int] = set()

    for position, start in enumerate(headings):
        match = ATX_HEADING.match(lines[start])
        if match is None or not _TOC_TITLE.match(match.group(2).strip()):
            continue
        end = headings[position + 1] if position + 1 < len(headings) else len(lines)
        if _looks_like_toc(lines[start + 1 : end]):
            drop.update(range(start, end))

    return "\n".join(
        line if in_code else _DOT_LEADER.sub("", line)
        for i, (line, in_code) in enumerate(zip(lines, fenced, strict=True))
        if i not in drop
    )


_TOC_MAX_LINES = 200
_ENDS_WITH_PAGE_NUMBER = re.compile(r"\s\d{1,3}\s*$")


def _looks_like_toc(body: list[str]) -> bool:
    filled = [line for line in body if line.strip()]
    if len(filled) > _TOC_MAX_LINES:
        return False
    toc_like = sum(
        1
        for line in filled
        if _DOT_LEADER.search(line) or _ENDS_WITH_PAGE_NUMBER.search(line)
    )
    return toc_like * 2 >= len(filled)


# --- 10. Пробелы -------------------------------------------------------------


def _normalize_whitespace(text: str) -> str:
    lines: list[str] = []
    for line in text.split("\n"):
        line = line.rstrip()
        body = line.lstrip(" ")
        indent = line[: len(line) - len(body)]
        lines.append(indent + re.sub(r" {2,}", " ", body))

    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n")
