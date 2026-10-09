"""Markdown из подключённых систем (База знаний 2.0, Яндекс Вики) — без
сырого HTML: теги и HTML-блоки вырезаются, видимый текст и разметка
Markdown остаются, код не трогается."""

import pytest

from corp_ed.ingest.extract import ExtractionError
from corp_ed.services.connector_sync_service import markdown_as_is


def test_plain_markdown_is_unchanged() -> None:
    text = (
        "# Отпуск\n"
        "\n"
        "## Как оформить\n"
        "\n"
        "1. Заявление за **две недели**.\n"
        "2. Согласовать с [руководителем](https://wiki.example.com/boss).\n"
        "   - подпись в _системе_\n"
        "\n"
        "> Цитата: a < b и c > d, 3 <= 4.\n"
        "\n"
        "| Тип | Дней |\n"
        "| --- | --- |\n"
        "| Основной | 28 |\n"
        "\n"
        "Адрес: <https://hr.example.com/vacation> или <hr@example.com>.\n"
        "Ссылка с пробелом: [бланк](<https://example.com/a b.pdf>).\n"
        "Относительная: [раздел](<a b>).\n"
        "\n"
        "![схема](https://example.com/s.png)\n"
        "\n"
        "---\n"
        "\n"
        "Код `<div>` и\n"
        "\n"
        "```python\n"
        "print('<b>')\n"
        "```"
    )
    assert markdown_as_is(text) == text


def test_script_is_removed_with_its_content() -> None:
    text = "До\n\n<script>alert('x')</script>\n\nПосле <script src=x></script>конец"
    assert markdown_as_is(text) == "До\n\n\n\nПосле конец"


def test_tags_go_away_visible_text_stays() -> None:
    assert markdown_as_is("Это <b>важно</b> и <span style='x'>тоже</span>.") == (
        "Это важно и тоже."
    )
    assert markdown_as_is("| <i>a</i> | b<br>c |\n| --- | --- |") == (
        "| a | b c |\n| --- | --- |"
    )


def test_image_with_handler_is_removed() -> None:
    assert markdown_as_is('Фото: <img src="x" onerror="alert(1)"> тут') == (
        "Фото:  тут"
    )
    # Атрибуты на нескольких строках и «>» внутри кавычек.
    text = 'Фото <img\n  alt="a>b"\n  onerror=alert(1)\n> тут'
    assert markdown_as_is(text) == "Фото  тут"


def test_iframe_and_similar_blocks_are_removed() -> None:
    text = (
        "Видео:\n"
        "\n"
        '<iframe src="https://example.com/v"></iframe>\n'
        "\n"
        "<object data=x>запасной текст</object><embed src=y>\n"
        "<style>body{display:none}</style>\n"
        "\n"
        "Текст"
    )
    assert markdown_as_is(text) == "Видео:\n\n\n\n\n\n\nТекст"


def test_html_comments_are_removed() -> None:
    assert markdown_as_is("а<!-- скрыто -->б") == "аб"
    text = "Начало\n<!--\nмного\nстрок <script>x</script>\n-->\nКонец"
    assert markdown_as_is(text) == "Начало\n\nКонец"


def test_html_block_keeps_its_text() -> None:
    text = (
        '<div class="note">\n<p>Важно: <a href="#" onclick="x()">ссылка</a></p>\n</div>'
    )
    assert markdown_as_is(text) == "Важно: ссылка"


def test_tags_inside_code_are_kept() -> None:
    text = (
        'Вставьте `<script src="app.js"></script>` в шаблон.\n'
        "\n"
        "```html\n"
        "<!-- комментарий -->\n"
        '<iframe src="x"></iframe>\n'
        "<img src=x onerror=alert(1)>\n"
        "```\n"
        "\n"
        "~~~~\n"
        "<b>тильды</b>\n"
        "```\n"
        "<i>всё ещё код</i>\n"
        "~~~~\n"
        "\n"
        "Двойные ``код с ` <u>внутри</u>`` и <u>снаружи</u>."
    )
    expected = text.replace(
        "Двойные ``код с ` <u>внутри</u>`` и <u>снаружи</u>.",
        "Двойные ``код с ` <u>внутри</u>`` и снаружи.",
    )
    assert markdown_as_is(text) == expected


def test_unclosed_fence_keeps_the_rest_as_code() -> None:
    text = "Текст <b>жирный</b>\n\n```\n<b>код до конца</b>"
    assert markdown_as_is(text) == "Текст жирный\n\n```\n<b>код до конца</b>"


def test_fence_inside_html_block_is_not_code() -> None:
    # По CommonMark блок HTML идёт до пустой строки: ограда внутри него —
    # не код, и тег внутри не должен уцелеть под видом примера.
    text = "<div>\n```\n<script>alert(1)</script>\n```\n</div>\n\nТекст"
    result = markdown_as_is(text)
    assert "<script" not in result
    assert "<div" not in result
    assert result.endswith("Текст")


def test_tag_that_starts_before_a_code_span_is_a_tag() -> None:
    # Тег начался раньше `кода` — по CommonMark это тег целиком.
    text = 'Ссылка <a title="`x`" onclick="y()">тут</a> и `<b>код</b>`'
    assert markdown_as_is(text) == "Ссылка тут и `<b>код</b>`"


def test_inline_code_does_not_span_paragraphs() -> None:
    text = "один ` обратный апостроф\n\n<script>alert(1)</script>\n\nещё `"
    assert "<script" not in markdown_as_is(text)


def test_split_tags_do_not_reassemble() -> None:
    assert "<script" not in markdown_as_is("<scr<b></b>ipt>alert(1)</script>")
    assert markdown_as_is("Фото <<b></b>img src=x onerror=alert(1)> тут") == (
        "Фото  тут"
    )


def test_deeply_split_tags_fail_closed() -> None:
    # Каждый проход снимает один слой; глубже предела — документ не
    # принимается, а не сохраняется недочищенным.
    depth = 12
    text = "Текст " + "<scr" * depth + "<b></b>" + "ipt>" * depth + "alert(1)"
    with pytest.raises(ExtractionError, match="corrupted"):
        markdown_as_is(text)
    # Неглубокая вложенность дочищается как обычно.
    shallow = "Текст " + "<scr" * 3 + "<b></b>" + "ipt>" * 3 + "конец"
    assert markdown_as_is(shallow) == "Текст конец"


def test_unclosed_hidden_tag_drops_only_the_tag() -> None:
    assert markdown_as_is("Тег <script> подключает код; дальше текст.") == (
        "Тег  подключает код; дальше текст."
    )


def test_processing_instructions_and_declarations_are_removed() -> None:
    text = "<?xml version='1.0'?>\n<!DOCTYPE html>\n<![CDATA[ x ]]>Текст"
    assert markdown_as_is(text) == "Текст"


def test_document_of_only_html_has_no_text() -> None:
    with pytest.raises(ExtractionError, match="no_text"):
        markdown_as_is("<script>alert('только скрипт')</script>\n<!-- и всё -->")


def test_windows_line_endings() -> None:
    assert markdown_as_is("а\r\n<b>б</b>\r\n```\r\n<b>в</b>\r\n```") == (
        "а\nб\n```\n<b>в</b>\n```"
    )
