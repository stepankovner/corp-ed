"""Нарезка текста на чанки.

Две версии живут рядом, пока бэкенд не переключится:

- split_into_chunks (v1) — текущая: абзацы → предложения по регулярке,
  размер в символах, заголовки не учитываются.
- split_document (v2, задача A3) — секции по заголовкам Markdown, размер
  в токенах, razdel для границ предложений, хлебные крошки и два
  представления текста: для эмбеддинга и для LLM.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from razdel import sentenize  # type: ignore[import-untyped]

from corp_ed.domain.markdown import (
    ATX_HEADING,
    clean_heading_text,
    fenced_lines,
    to_plain_text,
)
from corp_ed.domain.tokens import CHARS_PER_TOKEN, count_tokens

PARAGRAPH_SEPARATOR = "\n\n"
SENTENCE_SEPARATOR = " "
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text: str) -> list[str]:
    """Границы предложений"""
    return [s for s in _SENTENCE_BOUNDARY.split(text) if s.strip()]


def _pack(parts: list[str], limit: int, separator: str) -> list[str]:
    """Собрать куски в группы, не превышающие limit.

    Кусок, который сам длиннее limit, возвращается отдельной группой
    как есть: резать внутри куска — задача вызывающего.
    """
    result: list[str] = []
    buffer: list[str] = []

    for part in parts:
        if not buffer:
            buffer.append(part)
            continue

        if len(separator.join([*buffer, part])) <= limit:
            buffer.append(part)
        else:
            result.append(separator.join(buffer))
            buffer = [part]

    if buffer:
        result.append(separator.join(buffer))

    return result


def _apply_overlap(chunks: list[str], overlap: int) -> list[str]:
    """Добавить в начало каждого чанка хвост предыдущего.

    Хвост берётся из исходного соседа, а не из уже перекрытого, иначе
    перекрытия наслаивались бы и чанки росли к концу документа.
    Чанк становится длиннее chunk_size на overlap — это осознанно:
    chunk_size отвечает за объём нового текста, overlap за контекст слева.
    """
    if overlap <= 0 or len(chunks) < 2:
        return chunks

    result = [chunks[0]]
    for previous, current in zip(chunks, chunks[1:], strict=False):
        tail = previous[-overlap:]
        result.append(f"{tail} {current}")

    return result


def split_into_chunks(
    text: str,
    *,
    chunk_size: int,
    overlap: int,
) -> list[str]:
    paragraphs = [p.strip() for p in text.split(PARAGRAPH_SEPARATOR) if p.strip()]
    groups = _pack(paragraphs, chunk_size, PARAGRAPH_SEPARATOR)

    chunks: list[str] = []
    for group in groups:
        if len(group) <= chunk_size:
            chunks.append(group)
        else:
            sentences = _split_sentences(group)
            chunks.extend(_pack(sentences, chunk_size, SENTENCE_SEPARATOR))

    return _apply_overlap(chunks, overlap)


# === Нарезка v2 (задача A3) ==================================================
#
# Отличия от split_into_chunks:
# - документ сначала режется на секции по заголовкам Markdown; чанк никогда
#   не пересекает границу секции;
# - размер — в токенах (domain/tokens.py), а не в символах;
# - границы предложений ищет razdel, а не регулярка: «ст. 7 ТК РФ», «т. е.»,
#   инициалы не рвутся (риск №3);
# - предложение длиннее лимита режется по словам (риск №4), слово длиннее
#   лимита — по символам: тело чанка никогда не превышает chunk_tokens;
# - строка таблицы (после preprocess — «ключ: значение; …») не разрывается;
# - перекрытие — целыми предложениями с конца предыдущего чанка той же
#   секции; только если последнее предложение само длиннее перекрытия,
#   берётся хвост его слов;
# - у каждого чанка хлебные крошки «Документ > Раздел > Подраздел» и два
#   представления: embed_text (без разметки, в эмбеддинг) и llm_text
#   (Markdown, в промпт).

BREADCRUMB_SEPARATOR = " > "

_FILE_EXTENSION = re.compile(r"\.(?:docx?|pdf|txt|md|markdown|rtf|odt)$", re.IGNORECASE)
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+(?:\.\d+)*[.)])\s+")
_KEY_VALUE_ROW = re.compile(r"^[^:;]{1,120}: [^;]*(?:; [^:;]{1,120}: [^;]*)+$")

_NEW_BLOCK = "\n\n"
_NEW_LINE = "\n"
_SAME_LINE = " "


@dataclass(frozen=True)
class ChunkDraft:
    """Чанк, готовый к эмбеддингу и записи в chunks.

    position — сквозной номер чанка в документе, с 0.
    heading_path — стек заголовков секции без названия документа:
        ["Раздел 3", "3.2 Перенос отпуска"].
    embed_text — крошки + текст без разметки → в эмбеддинг (text-search-doc).
    llm_text — крошки + Markdown → в промпт LLM.
    """

    position: int
    heading_path: list[str]
    embed_text: str
    llm_text: str


@dataclass(frozen=True)
class SectionDraft:
    """Секция документа с дочерними чанками — для small-to-big (M2).

    Ищем по маленьким чанкам, в LLM отдаём секцию целиком (llm_text),
    если она влезает в бюджет контекста. Контракт черновой: схему
    таблицы sections бэкенд согласует на неделе 5.
    """

    position: int
    heading_path: list[str]
    llm_text: str
    chunks: list[ChunkDraft]


def format_breadcrumbs(title: str, heading_path: Sequence[str]) -> str:
    """«Документ > Раздел > Подраздел».

    Расширение файла в названии отбрасывается («Положение.docx» →
    «Положение»), подряд идущие одинаковые элементы схлопываются: название
    документа часто совпадает с его заголовком первого уровня.
    """
    parts: list[str] = []
    for raw in [_FILE_EXTENSION.sub("", title.strip()), *heading_path]:
        part = " ".join(raw.split())
        if part and (not parts or part.casefold() != parts[-1].casefold()):
            parts.append(part)
    return BREADCRUMB_SEPARATOR.join(parts)


def split_document(
    markdown: str,
    *,
    title: str,
    chunk_tokens: int,
    overlap_tokens: int,
) -> list[ChunkDraft]:
    """Нарезать Markdown (после ingest.preprocess) на чанки.

    chunk_tokens — максимум токенов в теле чанка, включая перекрытие.
    Крошки идут первой строкой и в этот лимит не входят: полный
    embed_text ≤ chunk_tokens + токены крошек.
    """
    sections = split_sections(
        markdown,
        title=title,
        chunk_tokens=chunk_tokens,
        overlap_tokens=overlap_tokens,
    )
    return [chunk for section in sections for chunk in section.chunks]


def split_sections(
    markdown: str,
    *,
    title: str,
    chunk_tokens: int,
    overlap_tokens: int,
) -> list[SectionDraft]:
    """То же, что split_document, но с группировкой чанков по секциям (M2)."""
    if chunk_tokens <= 0:
        raise ValueError("chunk_tokens must be positive")
    if not 0 <= overlap_tokens < chunk_tokens:
        raise ValueError("overlap_tokens must be in [0, chunk_tokens)")

    max_unit_tokens = chunk_tokens - overlap_tokens
    sections: list[SectionDraft] = []
    seen: set[str] = set()
    position = 0

    for raw in _parse_sections(markdown):
        units = [
            piece
            for unit in _section_units(raw.lines)
            for piece in _split_long_unit(unit, max_unit_tokens)
        ]
        if not units:
            continue

        crumbs = format_breadcrumbs(title, raw.heading_path)
        chunks: list[ChunkDraft] = []

        for body_units in _pack_units(units, chunk_tokens, overlap_tokens):
            body = _render(body_units)
            plain = to_plain_text(body)
            llm_text = _with_crumbs(crumbs, body)
            # Точный повтор (тот же раздел, тот же текст) — обычно форма,
            # продублированная в приложениях. Второй экземпляр только
            # занял бы место в top-k.
            if not plain or llm_text in seen:
                continue
            seen.add(llm_text)
            chunks.append(
                ChunkDraft(
                    position=position,
                    heading_path=list(raw.heading_path),
                    embed_text=_with_crumbs(crumbs, plain),
                    llm_text=llm_text,
                )
            )
            position += 1

        if chunks:
            sections.append(
                SectionDraft(
                    position=len(sections),
                    heading_path=list(raw.heading_path),
                    llm_text=_with_crumbs(crumbs, "\n".join(raw.lines).strip()),
                    chunks=chunks,
                )
            )

    return sections


# --- Разбор на секции ----------------------------------------------------------


@dataclass
class _RawSection:
    heading_path: list[str]
    lines: list[str] = field(default_factory=list)


def _parse_sections(markdown: str) -> list[_RawSection]:
    lines = markdown.split("\n")
    fenced = fenced_lines(lines)
    stack: list[tuple[int, str]] = []
    sections = [_RawSection(heading_path=[])]

    for line, in_code in zip(lines, fenced, strict=True):
        match = None if in_code else ATX_HEADING.match(line)
        if match is None:
            sections[-1].lines.append(line)
            continue

        heading = clean_heading_text(match.group(2))
        if not heading:
            continue
        level = len(match.group(1))
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, heading))
        sections.append(_RawSection(heading_path=[text for _, text in stack]))

    return sections


# --- Единицы упаковки ----------------------------------------------------------


@dataclass(frozen=True)
class _Unit:
    """Неделимый кусок текста и разделитель перед ним внутри чанка.

    atomic — строка таблицы или кода: её нельзя начинать с середины,
    поэтому хвост её слов в перекрытие не берётся.
    """

    text: str
    joiner: str
    atomic: bool = False


def _blocks(lines: list[str]) -> list[tuple[list[str], bool]]:
    """Абзацы (разделены пустой строкой) и признак «это блок кода»."""
    fenced = fenced_lines(lines)
    blocks: list[tuple[list[str], bool]] = []
    current: list[str] = []
    is_code = False

    for line, in_code in zip(lines, fenced, strict=True):
        if not line.strip() and not in_code:
            if current:
                blocks.append((current, is_code))
            current, is_code = [], False
            continue
        current.append(line.rstrip())
        is_code = is_code or in_code

    if current:
        blocks.append((current, is_code))
    return blocks


def _logical_lines(block: list[str], is_code: bool) -> list[tuple[str, bool]]:
    """Строки блока: (текст, неделимая ли).

    Неделимы строки кода и строки таблиц. Пункт списка начинает новую
    строку; обычные строки подряд склеиваются через пробел — так чинится
    жёсткий перенос в txt и продолжение пункта списка.
    """
    if is_code:
        return [(line, True) for line in block]

    result: list[tuple[str, bool]] = []
    for line in block:
        stripped = line.strip()
        atomic = bool(_KEY_VALUE_ROW.match(stripped)) or (
            stripped.startswith("|") and stripped.endswith("|")
        )
        starts_new = (
            atomic or not result or result[-1][1] or bool(_LIST_ITEM.match(line))
        )
        if starts_new:
            result.append((line, atomic))
        else:
            previous, _ = result[-1]
            result[-1] = (f"{previous} {stripped}", False)
    return result


def _sentences(text: str) -> list[str]:
    indent = text[: len(text) - len(text.lstrip())]
    parts: list[str] = [part.text for part in sentenize(text.strip())]
    if not parts:
        return [text]
    parts[0] = indent + parts[0]
    return parts


def _section_units(lines: list[str]) -> list[_Unit]:
    units: list[_Unit] = []
    for block, is_code in _blocks(lines):
        for line_index, (line, atomic) in enumerate(_logical_lines(block, is_code)):
            pieces = [line] if atomic else _sentences(line)
            for piece_index, piece in enumerate(pieces):
                if piece_index > 0:
                    joiner = _SAME_LINE
                elif line_index > 0:
                    joiner = _NEW_LINE
                else:
                    joiner = _NEW_BLOCK if units else ""
                units.append(_Unit(piece, joiner, atomic))
    return units


def _split_word(word: str, limit: int) -> list[str]:
    if count_tokens(word) <= limit:
        return [word]
    size = max(1, int(limit * CHARS_PER_TOKEN))
    return [word[start : start + size] for start in range(0, len(word), size)]


def _split_long_unit(unit: _Unit, limit: int) -> list[_Unit]:
    """Предложение (или строка таблицы) длиннее лимита → куски по словам."""
    if count_tokens(unit.text) <= limit:
        return [unit]

    pieces: list[str] = []
    current = ""
    for word in unit.text.split():
        for part in _split_word(word, limit):
            candidate = f"{current} {part}" if current else part
            if current and count_tokens(candidate) > limit:
                pieces.append(current)
                current = part
            else:
                current = candidate
    if current:
        pieces.append(current)

    return [
        _Unit(piece, unit.joiner if index == 0 else _SAME_LINE, unit.atomic)
        for index, piece in enumerate(pieces)
    ]


# --- Упаковка ------------------------------------------------------------------


def _render(units: list[_Unit]) -> str:
    parts: list[str] = []
    for index, unit in enumerate(units):
        if index:
            parts.append(unit.joiner or _SAME_LINE)
        parts.append(unit.text)
    return "".join(parts).strip()


def _tokens(units: list[_Unit]) -> int:
    return count_tokens(_render(units))


def _overlap_tail(units: list[_Unit], overlap_tokens: int) -> list[_Unit]:
    """Хвост чанка не длиннее overlap_tokens: целые предложения с конца.

    Если последнее предложение само длиннее перекрытия, берутся последние
    слова, которые влезают. Для строки таблицы — нет: обрывок строки
    («…; Сумма: 5 000 000» без номера заявки) вводит в заблуждение сильнее,
    чем отсутствие перекрытия.
    """
    if overlap_tokens <= 0 or not units:
        return []

    tail: list[_Unit] = []
    for unit in reversed(units):
        candidate = [unit, *tail]
        if _tokens(candidate) > overlap_tokens:
            break
        tail = candidate
    if tail or units[-1].atomic:
        return tail

    words: list[str] = []
    for word in reversed(units[-1].text.split()):
        candidate_words = [word, *words]
        if count_tokens(" ".join(candidate_words)) > overlap_tokens:
            break
        words = candidate_words
    return [_Unit(" ".join(words), _SAME_LINE)] if words else []


def _pack_units(
    units: list[_Unit], chunk_tokens: int, overlap_tokens: int
) -> list[list[_Unit]]:
    """Жадно упаковать единицы в чанки ≤ chunk_tokens с перекрытием.

    Каждая единица здесь уже ≤ chunk_tokens - overlap_tokens, поэтому
    перекрытие плюс следующая единица почти всегда влезают; если из-за
    разделителей не влезают — перекрытие укорачивается с начала.
    """
    chunks: list[list[_Unit]] = []
    current: list[_Unit] = []
    fresh = 0

    for unit in units:
        if fresh and _tokens([*current, unit]) > chunk_tokens:
            chunks.append(current)
            current = _overlap_tail(current, overlap_tokens)
            while current and _tokens([*current, unit]) > chunk_tokens:
                current = current[1:]
            fresh = 0
        current.append(unit)
        fresh += 1

    if fresh:
        chunks.append(current)
    return chunks


def _with_crumbs(crumbs: str, body: str) -> str:
    return f"{crumbs}\n{body}" if crumbs else body
