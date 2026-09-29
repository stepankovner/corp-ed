"""Общие утилиты для Markdown: заголовки, выделение, блоки кода, чистый текст.

Нужны и предобработке (ingest/preprocess.py), и нарезке (domain/split.py),
поэтому живут в domain, а не в ingest: domain не должен зависеть от ingest.
"""

import re

ATX_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
"""Заголовок «# …» — «###### …». Группа 1 — решётки, группа 2 — текст."""

_FENCE = re.compile(r"^\s*(```|~~~)")

_EMPHASIS_STRONG = re.compile(r"(\*\*|__)(.+?)\1")
_EMPHASIS_UNDERSCORE = re.compile(r"(?<![\w\\])_(?!\s)(.+?)(?<!\s)_(?!\w)")
_EMPHASIS_STAR = re.compile(r"(?<![\w*\\])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")

_HEADING_MARK = re.compile(r"^\s{0,3}#{1,6}[ \t]+")
_BLOCKQUOTE = re.compile(r"^\s*(?:>\s?)+")
_BULLET = re.compile(r"^(\s*)[-*+][ \t]+")
_HORIZONTAL_RULE = re.compile(r"^\s*(?:[-*_][ \t]*){3,}$")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")


def strip_emphasis(text: str) -> str:
    """Убрать выделение **жирный**, __жирный__, *курсив*, _курсив_.

    Подчёркивание внутри слова (tenant_id, file_name) не трогается.
    """
    text = _EMPHASIS_STRONG.sub(r"\2", text)
    text = _EMPHASIS_UNDERSCORE.sub(r"\1", text)
    text = _EMPHASIS_STAR.sub(r"\1", text)
    return text.replace("**", "").replace("__", "")


def clean_heading_text(text: str) -> str:
    """Текст заголовка без разметки и лишних пробелов."""
    return " ".join(strip_emphasis(text).replace("`", "").split())


def fenced_lines(lines: list[str]) -> list[bool]:
    """Для каждой строки: лежит ли она внутри блока кода ``` / ~~~.

    Строки-ограждения сами считаются частью блока. Внутри кода «# …» —
    это комментарий, а не заголовок, и трогать его нельзя.
    """
    result: list[bool] = []
    inside = False
    for line in lines:
        if _FENCE.match(line):
            result.append(True)
            inside = not inside
        else:
            result.append(inside)
    return result


def to_plain_text(markdown: str) -> str:
    """Markdown → текст без символов разметки (для эмбеддинга).

    Битрикс24: для эмбеддинга «чистый» текст стабильно обходил Markdown —
    **, #, | для модели шум. Нумерация списков («1.», «3.2») сохраняется:
    это содержание («пункт 3.2»), а не разметка. Маркеры «-», «*»
    убираются.
    """
    lines: list[str] = []
    for line in markdown.split("\n"):
        if _FENCE.match(line) or _HORIZONTAL_RULE.match(line):
            continue
        line = _HEADING_MARK.sub("", line)
        line = _BLOCKQUOTE.sub("", line)
        line = _BULLET.sub(r"\1", line)
        line = _IMAGE.sub(r"\1", line)
        line = _LINK.sub(r"\1", line)
        line = _HTML_TAG.sub(" ", line)
        line = strip_emphasis(line).replace("`", "").replace("|", " ")
        lines.append(" ".join(line.split()))

    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
