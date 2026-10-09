"""ProseMirror JSON → Markdown для документов Kaiten.

API Kaiten отдаёт содержимое документа (`data`) только в ProseMirror
JSON — ни HTML, ни Markdown (developers.kaiten.ru, 09.10). Имена узлов
документация не перечисляет (схема — отдельным запросом
`/document-schemas/latest`), поэтому имена сравниваются без регистра и
подчёркиваний: `bullet_list` (prosemirror-schema-basic) и `bulletList`
(TipTap) — один узел.

Что переводится: заголовки, абзацы, списки (маркированные, нумерованные,
задачи), цитаты, код, таблицы, разделитель, перенос строки; отметки —
жирный, курсив, код, зачёркнутый, ссылка (только http, https, mailto).
Картинки — их подписью, без адреса: внешних загрузок у документа нет.
Неизвестный узел — его текст без разметки (или подпись из attrs для
упоминаний), неизвестные отметки отбрасываются.

Документ пишет кто угодно в компании клиента: на входе может быть что
угодно. Не тот тип значения — пропуск узла, вложенность глубже
MAX_DEPTH — обрезается. Сырой HTML здесь не создаётся, а тот, что
пришёл текстом, вырежет общий конвейер (connectors/markdown.py).
"""

import json
import re
from typing import Any
from urllib.parse import quote, urlsplit

MAX_DEPTH = 48
_SAFE_SCHEMES = frozenset({"http", "https", "mailto"})
# Символы, которые остаются в адресе ссылки как есть; пробел и скобки
# кодируются — иначе ссылка закрылась бы раньше времени.
_URL_SAFE = ":/?#[]@!$&'*+,;=%-._~"
_LANGUAGE = re.compile(r"[^\w+.-]")
# Узлы, которые стоят внутри абзаца (строчные), — остальные блочные.
_INLINE = frozenset({"text", "hardbreak", "image", "mention", "emoji"})
# Подпись строчного узла без текста (упоминание, эмодзи) — из attrs.
_LABEL_ATTRS = ("label", "text", "title", "name", "alt")


def prosemirror_to_markdown(data: Any, *, title: str | None = None) -> str:
    """Документ (объект или строка JSON) → Markdown. Мусор — пустая строка.

    title — заголовок документа: ставится первой строкой, если документ
    не начинается с него сам.
    """
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return ""
    if not isinstance(data, dict):
        return ""
    return with_title(_Renderer().block(data, 0).strip(), title)


def with_title(body: str, title: str | None) -> str:
    """Заголовок документа первой строкой, если тело не начинается с него."""
    clean_title = " ".join((title or "").split())
    if not clean_title:
        return body
    first = body.split("\n", 1)[0]
    if first.startswith("#") and first.lstrip("#").strip() == clean_title:
        return body
    return f"# {clean_title}\n\n{body}".strip()


def _kind(node: dict[str, Any]) -> str:
    value = node.get("type")
    if not isinstance(value, str):
        return ""
    return value.lower().replace("_", "").replace("-", "")


def _attrs(node: dict[str, Any]) -> dict[str, Any]:
    attrs = node.get("attrs")
    return attrs if isinstance(attrs, dict) else {}


def _children(node: dict[str, Any]) -> list[dict[str, Any]]:
    content = node.get("content")
    if not isinstance(content, list):
        return []
    return [child for child in content if isinstance(child, dict)]


def _is_inline(node: dict[str, Any]) -> bool:
    kind = _kind(node)
    if kind in _INLINE:
        return True
    if kind in _BLOCK_KINDS:
        return False
    # Неизвестный узел строчный, если у него свой текст или подпись без
    # содержимого (упоминание), либо всё содержимое — строчное.
    children = _children(node)
    if not children:
        return True
    return all(_is_inline(child) for child in children)


class _Renderer:
    def block(self, node: dict[str, Any], depth: int) -> str:
        """Блочный узел → Markdown без завершающего перевода строки."""
        if depth > MAX_DEPTH:
            return ""
        kind = _kind(node)
        attrs = _attrs(node)
        if kind == "heading":
            level = attrs.get("level")
            level = level if isinstance(level, int) else 1
            level = min(max(level, 1), 6)
            text = self.inline(_children(node), depth).replace("\n", " ").strip()
            return f"{'#' * level} {text}" if text else ""
        if kind == "paragraph":
            return self.inline(_children(node), depth).strip()
        if kind in {"bulletlist", "orderedlist", "tasklist"}:
            return self._list(node, kind, depth)
        if kind == "blockquote":
            inner = self.blocks(_children(node), depth)
            return (
                "\n".join(f"> {line}" if line else ">" for line in inner.split("\n"))
                if inner
                else ""
            )
        if kind == "codeblock":
            return self._code(node)
        if kind == "horizontalrule":
            return "---"
        if kind == "table":
            return self._table(node, depth)
        # doc, listItem вне списка, неизвестные контейнеры — по содержимому.
        if _is_inline(node):
            return self.inline([node], depth - 1).strip()
        return self.blocks(_children(node), depth)

    def blocks(self, nodes: list[dict[str, Any]], depth: int, sep: str = "\n\n") -> str:
        """Последовательность узлов: подряд идущие строчные — один абзац."""
        parts: list[str] = []
        run: list[dict[str, Any]] = []

        def flush() -> None:
            if run:
                text = self.inline(run, depth).strip()
                if text:
                    parts.append(text)
                run.clear()

        for child in nodes:
            if _is_inline(child):
                run.append(child)
                continue
            flush()
            rendered = self.block(child, depth + 1)
            if rendered.strip():
                parts.append(rendered)
        flush()
        return sep.join(parts)

    def inline(self, nodes: list[dict[str, Any]], depth: int) -> str:
        if depth > MAX_DEPTH:
            return ""
        out: list[str] = []
        for node in nodes:
            kind = _kind(node)
            if kind == "text":
                value = node.get("text")
                if isinstance(value, str):
                    out.append(_marked(value, node.get("marks")))
            elif kind == "hardbreak":
                out.append("  \n")
            elif kind == "image":
                alt = _attrs(node).get("alt") or _attrs(node).get("title")
                if isinstance(alt, str):
                    out.append(" ".join(alt.split()))
            elif _children(node):
                out.append(self.inline(_children(node), depth + 1))
            else:
                out.append(_label(node))
        return "".join(out)

    def _list(self, node: dict[str, Any], kind: str, depth: int) -> str:
        start = _attrs(node).get("start", _attrs(node).get("order", 1))
        number = start if isinstance(start, int) and 0 <= start < 10**9 else 1
        lines: list[str] = []
        for item in _children(node):
            if kind == "orderedlist":
                marker = f"{number}. "
                number += 1
            elif kind == "tasklist" or "checked" in _attrs(item):
                marker = "- [x] " if _attrs(item).get("checked") is True else "- [ ] "
            else:
                marker = "- "
            body = self.blocks(_children(item), depth + 1, sep="\n")
            if _kind(item) not in {"listitem", "taskitem"} and not body:
                body = self.block(item, depth + 1)
            body_lines = body.split("\n") if body else [""]
            indent = " " * len(marker) if kind != "tasklist" else "  "
            lines.append(f"{marker}{body_lines[0]}".rstrip())
            lines.extend(f"{indent}{line}" if line else "" for line in body_lines[1:])
        return "\n".join(lines)

    def _code(self, node: dict[str, Any]) -> str:
        code = "".join(
            str(child.get("text"))
            for child in _children(node)
            if isinstance(child.get("text"), str)
        )
        attrs = _attrs(node)
        language = attrs.get("language") or attrs.get("params") or ""
        language = _LANGUAGE.sub("", str(language))[:32]
        longest = max((len(run) for run in re.findall(r"`+", code)), default=0)
        fence = "`" * max(3, longest + 1)
        return f"{fence}{language}\n{code}\n{fence}"

    def _table(self, node: dict[str, Any], depth: int) -> str:
        rows: list[list[str]] = []
        for row in _children(node):
            cells: list[str] = []
            for cell in _children(row):
                text = self.blocks(_children(cell), depth + 2, sep=" ")
                text = " ".join(text.split()).replace("|", "\\|")
                cells.append(text)
                span = _attrs(cell).get("colspan")
                if isinstance(span, int) and 1 < span <= 64:
                    cells.extend([""] * (span - 1))
            if cells:
                rows.append(cells)
        if not rows:
            return ""
        width = max(len(cells) for cells in rows)
        lines = []
        for index, cells in enumerate(rows):
            padded = cells + [""] * (width - len(cells))
            lines.append("| " + " | ".join(padded) + " |")
            if index == 0:
                lines.append("| " + " | ".join(["---"] * width) + " |")
        return "\n".join(lines)


_BLOCK_KINDS = frozenset(
    {
        "doc",
        "paragraph",
        "heading",
        "bulletlist",
        "orderedlist",
        "tasklist",
        "listitem",
        "taskitem",
        "blockquote",
        "codeblock",
        "horizontalrule",
        "table",
        "tablerow",
        "tablecell",
        "tableheader",
    }
)


def _label(node: dict[str, Any]) -> str:
    attrs = _attrs(node)
    for key in _LABEL_ATTRS:
        value = attrs.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return ""


def _marked(text: str, marks: Any) -> str:
    """Текст с отметками; пробелы по краям — снаружи маркеров."""
    if not isinstance(marks, list) or not text.strip():
        return text
    kinds = {_kind(m): m for m in marks if isinstance(m, dict)}
    leading = text[: len(text) - len(text.lstrip())]
    trailing = text[len(text.rstrip()) :]
    core = text.strip()
    if "code" in kinds:
        longest = max((len(run) for run in re.findall(r"`+", core)), default=0)
        ticks = "`" * (longest + 1)
        pad = " " if core.startswith("`") or core.endswith("`") else ""
        core = f"{ticks}{pad}{core}{pad}{ticks}"
    else:
        if kinds.keys() & {"strike", "strikethrough", "s"}:
            core = f"~~{core}~~"
        if kinds.keys() & {"em", "italic"}:
            core = f"*{core}*"
        if kinds.keys() & {"strong", "bold"}:
            core = f"**{core}**"
    link = kinds.get("link")
    if link is not None:
        href = _safe_href(_attrs(link).get("href"))
        if href:
            core = f"[{core}]({href})"
    return f"{leading}{core}{trailing}"


def _safe_href(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    href = value.strip()
    if urlsplit(href).scheme.lower() not in _SAFE_SCHEMES:
        return None
    return quote(href, safe=_URL_SAFE)
