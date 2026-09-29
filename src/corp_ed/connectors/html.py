"""HTML страницы источника → Markdown для конвейера ингеста.

Страница пришла из системы клиента, и в ней может быть что угодно:
скрипты, формы, встроенные фреймы, внешние картинки. Всё это
отбрасывается ДО markdownify — в Markdown уходит только текст и
структура (заголовки, списки, таблицы, ссылки). Никаких сетевых
загрузок при разборе нет: разбирается строка, а не документ по URL.
"""

import re

from bs4 import BeautifulSoup
from markdownify import markdownify

from corp_ed.ingest.extract import MAX_EXTRACTED_CHARS, ExtractionError

# Теги, чьё содержимое — не текст документа.
_DROP = (
    "script",
    "style",
    "noscript",
    "iframe",
    "frame",
    "object",
    "embed",
    "applet",
    "form",
    "input",
    "button",
    "select",
    "textarea",
    "svg",
    "canvas",
    "video",
    "audio",
    "template",
    "head",
    "nav",
    "footer",
)
# Разметка Confluence/Битрикс24 хранит макросы и метаданные в
# data-атрибутах и inline-стилях: в тексте они не нужны.
_KEEP_ATTRS = {"href", "src", "alt", "title", "colspan", "rowspan"}
MAX_HTML_BYTES = 20 * 1024 * 1024


def html_to_markdown(html: str) -> str:
    """Очистить HTML и перевести в Markdown. Пусто — ExtractionError('no_text')."""
    if len(html) > MAX_HTML_BYTES:
        raise ExtractionError("document_too_large")
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(_DROP):
        tag.decompose()
    for element in soup.find_all(True):
        for name in list(element.attrs):
            if name not in _KEEP_ATTRS or name.lower().startswith("on"):
                del element.attrs[name]
        # javascript: и data: в ссылках — не ссылки.
        href = element.attrs.get("href")
        if isinstance(href, str) and not re.match(
            r"^(https?:|mailto:|/|#)", href.strip(), re.IGNORECASE
        ):
            del element.attrs["href"]
        src = element.attrs.get("src")
        if isinstance(src, str) and not re.match(
            r"^(https?:|/)", src.strip(), re.IGNORECASE
        ):
            del element.attrs["src"]
    body = soup.body or soup
    markdown = markdownify(str(body), heading_style="ATX", strip=["img"])
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
    if len(markdown) > MAX_EXTRACTED_CHARS:
        raise ExtractionError("document_too_large")
    if not re.search(r"\w", markdown):
        raise ExtractionError("no_text")
    return markdown
