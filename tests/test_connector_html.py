"""HTML страниц источника → Markdown: только текст и структура."""

import pytest

from corp_ed.connectors.html import MAX_HTML_BYTES, html_to_markdown
from corp_ed.ingest.extract import ExtractionError

PAGE = """
<html><head><title>Регламент</title><style>h1{color:red}</style>
<script>alert(1)</script></head>
<body onload="steal()">
<nav>Меню</nav>
<h1 class="x" data-macro="toc">Отпуска</h1>
<p>Заявление подаётся <b>за две недели</b>.</p>
<script>document.cookie</script>
<form action="/x"><input name="q"><button>Отправить</button></form>
<iframe src="https://evil.example/"></iframe>
<ul><li>Первый</li><li>Второй</li></ul>
<a href="javascript:alert(1)">клик</a>
<a href="https://portal.example.com/doc/1">документ</a>
<img src="data:image/png;base64,AAAA" alt="картинка">
<table><tr><th>Дней</th></tr><tr><td>28</td></tr></table>
<footer>© Компания</footer>
</body></html>
"""


def test_markdown_keeps_text_and_structure_only() -> None:
    markdown = html_to_markdown(PAGE)
    assert markdown.startswith("# Отпуска")
    assert "**за две недели**" in markdown
    assert "* Первый" in markdown and "* Второй" in markdown
    assert "[документ](https://portal.example.com/doc/1)" in markdown
    assert "| Дней |" in markdown and "28" in markdown
    for forbidden in (
        "alert",
        "cookie",
        "steal",
        "Меню",
        "Отправить",
        "evil",
        "javascript:",
        "data:image",
        "©",
        "color:red",
    ):
        assert forbidden not in markdown, forbidden


def test_empty_page_is_rejected() -> None:
    with pytest.raises(ExtractionError) as exc:
        html_to_markdown("<html><body><script>x()</script><p>   </p></body></html>")
    assert exc.value.code == "no_text"


def test_huge_page_is_rejected_before_parsing() -> None:
    with pytest.raises(ExtractionError) as exc:
        html_to_markdown("<p>x</p>" + " " * (MAX_HTML_BYTES + 1))
    assert exc.value.code == "document_too_large"


def test_relative_and_anchor_links_survive_absolute_others_dropped() -> None:
    markdown = html_to_markdown(
        '<p><a href="/wiki/x">внутренняя</a> <a href="#top">якорь</a> '
        '<a href="mailto:a@b.ru">почта</a> <a href="file:///etc/passwd">файл</a></p>'
    )
    assert "[внутренняя](/wiki/x)" in markdown
    assert "[якорь](#top)" in markdown
    assert "[почта](mailto:a@b.ru)" in markdown
    assert "file:" not in markdown
