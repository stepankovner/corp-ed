"""Markdown из системы клиента — без сырого HTML.

База знаний 2.0 Битрикс24 и Яндекс Вики отдают Markdown, а Markdown
допускает сырой HTML: <script>, <iframe>, <img onerror=…>, комментарии.
Документ пишет кто угодно в компании клиента, и такие куски не должны
попасть ни в фрагменты для поиска, ни в ответ — ни разметкой (если её
когда-нибудь отрисуют как HTML), ни «текстом». Теги вырезаются, видимый
текст остаётся: «<b>важно</b>» → «важно»; содержимое <script>, <style>,
<iframe> и подобных — не текст документа и уходит вместе с тегом.

Код не трогается: внутри ограждённого блока (``` или ~~~) и `inline`
теги — это пример, а не разметка. Границы кода и HTML — как у CommonMark,
иначе тег, «спрятанный» в недоделанном коде, прошёл бы мимо:

- блок HTML (строка начинается с <div>, <script>, <!-- и т. п.) идёт до
  пустой строки или своего конца, и ограда внутри него — не код;
- тег, начатый раньше `кода`, — тег целиком (кодовый спан в атрибуте
  его не защищает);
- кодовый спан защищается только в пределах строки.

Лишнее удаление безопаснее пропуска: код с отступом в 4 пробела не
распознаётся, и теги в нём вырезаются. Проход повторяется, пока текст
меняется: «<scr<b></b>ipt>» не соберётся в живой тег. Сетевых загрузок
нет — разбирается строка.
"""

import re
from collections.abc import Callable

# Элементы, чьё содержимое — не текст документа (как _DROP в html.py).
_HIDDEN = frozenset(
    {
        "script",
        "style",
        "noscript",
        "iframe",
        "frame",
        "frameset",
        "object",
        "embed",
        "applet",
        "svg",
        "math",
        "template",
        "textarea",
        "select",
        "title",
        "head",
    }
)
# Имена блоков HTML (CommonMark, тип 6): с такой строки начинается блок.
_BLOCK_NAMES = (
    "address|article|aside|base|basefont|blockquote|body|caption|center|col|"
    "colgroup|dd|details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|"
    "footer|form|frame|frameset|h[1-6]|head|header|hr|html|iframe|legend|li|"
    "link|main|menu|menuitem|nav|noframes|ol|optgroup|option|p|param|search|"
    "section|summary|table|tbody|td|tfoot|th|thead|title|tr|track|ul"
)
_ATTR = (
    r"(?:\s+[A-Za-z_:][\w.:-]*"
    r"""(?:\s*=\s*(?:[^\s"'=<>`]+|'[^']*'|"[^"]*"))?)"""
)
_OPEN_TAG = rf"<(?P<open>[A-Za-z][A-Za-z0-9-]*){_ATTR}*\s*/?>"
_CLOSE_TAG = r"</[A-Za-z][A-Za-z0-9-]*\s*>"
# Кодовый спан: серия из n обратных апострофов до такой же серии в той же
# строке. Длина ограничена — патологическая строка не займёт воркер.
_CODE_SPAN = r"(?P<code>(?<!`)(?P<ticks>`+)(?!`)[^\n]{1,2000}?(?<!`)(?P=ticks)(?!`))"
# Адрес ссылки в угловых скобках — [текст](<адрес с пробелом>) — не тег.
_LINK_DESTINATION = r"(?P<dest>\]\(<[^<>\n]*>)"
_HTML = (
    r"(?P<comment><!--)"
    r"|(?P<cdata><!\[CDATA\[)"
    r"|(?P<pi><\?)"
    r"|(?P<decl><![A-Za-z][^>]*>)"
    r"|(?P<br><br\s*/?>)"
    rf"|{_OPEN_TAG}"
    rf"|(?P<close>{_CLOSE_TAG})"
)
_INLINE = re.compile(rf"{_CODE_SPAN}|{_LINK_DESTINATION}|{_HTML}", re.IGNORECASE)
# Внутри блока HTML Markdown не разбирается: обратные апострофы — не код.
_IN_BLOCK = re.compile(_HTML, re.IGNORECASE)
# Конец комментария, CDATA и инструкции; нет конца — снимается только
# открывающая метка (по CommonMark это просто текст).
_ENDS = {"comment": "-->", "cdata": "]]>", "pi": "?>"}

# Префикс строки внутри цитаты или пункта списка.
_CONTAINER = r"^(?:[ \t]*(?:>|[-+*](?=[ \t])|\d{1,9}[.)](?=[ \t])))*[ \t]*"
_FENCE = re.compile(rf"{_CONTAINER}(?P<fence>`{{3,}}|~{{3,}})(?P<info>.*)$")
# Начало блока HTML и признак его конца в строке; None — до пустой строки.
_BLOCK_STARTS: tuple[tuple[re.Pattern[str], re.Pattern[str] | None], ...] = (
    (
        re.compile(rf"{_CONTAINER}<(?:script|pre|style|textarea)(?:[\s>]|$)", re.I),
        re.compile(r"</(?:script|pre|style|textarea)\s*>", re.I),
    ),
    (re.compile(rf"{_CONTAINER}<!--"), re.compile(r"-->")),
    (re.compile(rf"{_CONTAINER}<\?"), re.compile(r"\?>")),
    (re.compile(rf"{_CONTAINER}<!\[CDATA\["), re.compile(r"\]\]>")),
    (re.compile(rf"{_CONTAINER}<![A-Za-z]"), re.compile(r">")),
    (re.compile(rf"{_CONTAINER}</?(?:{_BLOCK_NAMES})(?:[\s>]|/>|$)", re.I), None),
    (re.compile(rf"{_CONTAINER}(?:{_OPEN_TAG}|{_CLOSE_TAG})[ \t]*$", re.I), None),
)
_BLANK = re.compile(r"^[ \t]*$")
MAX_PASSES = 8


def strip_raw_html(markdown: str) -> str:
    """Вырезать сырой HTML из Markdown, не трогая код и разметку."""
    text = re.sub(r"\r\n?", "\n", markdown)
    for _ in range(MAX_PASSES):
        cleaned = _strip_once(text)
        if cleaned == text:
            break
        text = cleaned
    return text


def _strip_once(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    prose: list[str] = []

    def flush() -> None:
        if prose:
            out.append(_clean("\n".join(prose), _INLINE))
            prose.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE.match(line)
        if fence is not None and not (
            fence["fence"][0] == "`" and "`" in fence["info"]
        ):
            flush()
            end = _fence_end(lines, i + 1, fence["fence"])
            out.append("\n".join(lines[i:end]))
            i = end
            continue
        block_end = _html_block_end(lines, i)
        if block_end is not None:
            flush()
            out.append(_clean("\n".join(lines[i:block_end]), _IN_BLOCK))
            i = block_end
            continue
        prose.append(line)
        i += 1
    flush()
    return "\n".join(out)


def _fence_end(lines: list[str], start: int, fence: str) -> int:
    """Индекс строки после закрывающей ограды; нет её — код до конца."""
    closing = re.compile(rf"{_CONTAINER}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$")
    for j in range(start, len(lines)):
        if closing.match(lines[j]):
            return j + 1
    return len(lines)


def _html_block_end(lines: list[str], start: int) -> int | None:
    """Индекс строки после блока HTML, если он начинается здесь."""
    for begins, ends in _BLOCK_STARTS:
        if not begins.match(lines[start]):
            continue
        if ends is None:
            j = start + 1
            while j < len(lines) and not _BLANK.match(lines[j]):
                j += 1
            return j
        for j in range(start, len(lines)):
            if ends.search(lines[j]):
                return j + 1
        return len(lines)
    return None


def _clean(text: str, pattern: re.Pattern[str]) -> str:
    """Один проход по тексту: код оставить, HTML снять.

    Поиск слева направо — что началось раньше, то и есть (как в
    CommonMark): тег поглощает кодовый спан в своём атрибуте, кодовый
    спан — тег внутри себя. Конец скрытого элемента или комментария
    ищется отдельно: если его нет, следующий такой же тег не будет
    искать его снова — проход остаётся линейным.
    """
    out: list[str] = []
    missing: set[str] = set()
    pos = 0
    while (match := pattern.search(text, pos)) is not None:
        out.append(text[pos : match.start()])
        pos = match.end()
        kind = match.lastgroup
        if kind in ("code", "ticks", "dest"):
            out.append(match.group(0))
        elif kind == "br":
            out.append(" ")
        elif kind in _ENDS:
            pos = _skip_to(text, pos, _ENDS[kind], missing, _find_text)
        elif match["open"] is not None and match["open"].lower() in _HIDDEN:
            name = match["open"].lower()
            pos = _skip_to(text, pos, name, missing, _find_closing)
    out.append(text[pos:])
    return "".join(out)


def _skip_to(
    text: str,
    pos: int,
    key: str,
    missing: set[str],
    find: Callable[[str, str, int], int],
) -> int:
    """Позиция после конца конструкции; нет конца — снять только начало."""
    if key in missing:
        return pos
    end = find(text, key, pos)
    if end < 0:
        missing.add(key)
        return pos
    return end


def _find_text(text: str, marker: str, pos: int) -> int:
    end = text.find(marker, pos)
    return -1 if end < 0 else end + len(marker)


def _find_closing(text: str, name: str, pos: int) -> int:
    closing = re.compile(rf"</{name}\s*>", re.IGNORECASE).search(text, pos)
    return -1 if closing is None else closing.end()
