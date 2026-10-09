"""ProseMirror JSON (документы Kaiten) → Markdown: узлы обеих схем
(snake_case prosemirror-schema-basic и camelCase TipTap), отметки,
таблицы, неизвестные узлы и враждебный ввод."""

import json
from typing import Any

import pytest

from corp_ed.connectors.kaiten.prosemirror import MAX_DEPTH, prosemirror_to_markdown


def doc(*content: dict[str, Any]) -> dict[str, Any]:
    return {"type": "doc", "content": list(content)}


def text(value: str, *marks: str | dict[str, Any]) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "text": value}
    if marks:
        node["marks"] = [{"type": m} if isinstance(m, str) else m for m in marks]
    return node


def para(*content: dict[str, Any]) -> dict[str, Any]:
    return {"type": "paragraph", "content": list(content)}


def heading(level: Any, *content: dict[str, Any]) -> dict[str, Any]:
    return {"type": "heading", "attrs": {"level": level}, "content": list(content)}


def item(*content: dict[str, Any], kind: str = "list_item") -> dict[str, Any]:
    return {"type": kind, "content": list(content)}


def test_headings_and_paragraphs() -> None:
    markdown = prosemirror_to_markdown(
        doc(
            heading(1, text("Отпуск")),
            para(
                text("Сотрудник имеет право на "), text("28 дней", "strong"), text(".")
            ),
            heading(3, text("Порядок")),
            para(),
            para(text("Заявление — за две недели.")),
        )
    )
    assert markdown == (
        "# Отпуск\n\nСотрудник имеет право на **28 дней**.\n\n"
        "### Порядок\n\nЗаявление — за две недели."
    )


@pytest.mark.parametrize(("level", "hashes"), [(0, "#"), (9, "######"), ("x", "#")])
def test_heading_level_is_clamped(level: Any, hashes: str) -> None:
    assert prosemirror_to_markdown(doc(heading(level, text("Т")))) == f"{hashes} Т"


def test_marks_bold_italic_code_strike_link() -> None:
    markdown = prosemirror_to_markdown(
        doc(
            para(
                text("жирный", "bold"),
                text(" и "),
                text("курсив", "em"),
                text(", "),
                text("ещё", "italic", "strong"),
                text(", "),
                text("a`b", "code"),
                text(", "),
                text("старое", "strike"),
                text(", "),
                text(
                    "сайт", {"type": "link", "attrs": {"href": "https://example.com/a"}}
                ),
                text(", "),
                text("подчёркнуто", "underline"),
            )
        )
    )
    assert markdown == (
        "**жирный** и *курсив*, ***ещё***, ``a`b``, ~~старое~~, "
        "[сайт](https://example.com/a), подчёркнуто"
    )


def test_marks_keep_spaces_outside_markers() -> None:
    markdown = prosemirror_to_markdown(
        doc(para(text("до "), text(" важно ", "strong"), text("после")))
    )
    assert markdown == "до  **важно** после"


def test_unsafe_link_keeps_only_text() -> None:
    markdown = prosemirror_to_markdown(
        doc(
            para(
                text(
                    "клик", {"type": "link", "attrs": {"href": "javascript:alert(1)"}}
                ),
                text(" "),
                text("почта", {"type": "link", "attrs": {"href": "mailto:hr@x.ru"}}),
                text(" "),
                text(
                    "скобка", {"type": "link", "attrs": {"href": "https://x.ru/a b)"}}
                ),
            )
        )
    )
    assert markdown == "клик [почта](mailto:hr@x.ru) [скобка](https://x.ru/a%20b%29)"


def test_bullet_and_ordered_lists_snake_and_camel_case() -> None:
    nested = {
        "type": "bulletList",
        "content": [item(para(text("вложенный")), kind="listItem")],
    }
    markdown = prosemirror_to_markdown(
        doc(
            {
                "type": "bullet_list",
                "content": [
                    item(para(text("первый"))),
                    item(
                        para(text("второй")),
                        {"type": "bullet_list", "content": [item(para(text("под")))]},
                    ),
                ],
            },
            {
                "type": "orderedList",
                "attrs": {"start": 3},
                "content": [
                    item(para(text("три")), kind="listItem"),
                    item(para(text("четыре")), nested, kind="listItem"),
                ],
            },
        )
    )
    assert markdown == (
        "- первый\n- второй\n  - под\n\n3. три\n4. четыре\n   - вложенный"
    )


def test_task_list() -> None:
    markdown = prosemirror_to_markdown(
        doc(
            {
                "type": "taskList",
                "content": [
                    {
                        "type": "taskItem",
                        "attrs": {"checked": True},
                        "content": [para(text("сделано"))],
                    },
                    {
                        "type": "task_item",
                        "attrs": {"checked": False},
                        "content": [para(text("в работе"))],
                    },
                ],
            }
        )
    )
    assert markdown == "- [x] сделано\n- [ ] в работе"


def test_code_block_fence_is_longer_than_content() -> None:
    markdown = prosemirror_to_markdown(
        doc(
            {
                "type": "code_block",
                "attrs": {"language": "python"},
                "content": [text("print('a')\n```\nx = 1")],
            },
            {"type": "codeBlock", "content": [text("<b>не тег</b>")]},
        )
    )
    assert markdown == (
        "````python\nprint('a')\n```\nx = 1\n````\n\n```\n<b>не тег</b>\n```"
    )


def test_code_block_language_cannot_break_the_fence() -> None:
    markdown = prosemirror_to_markdown(
        doc(
            {
                "type": "code_block",
                "attrs": {"language": "py`\n# x"},
                "content": [text("1")],
            }
        )
    )
    assert markdown == "```pyx\n1\n```"


def test_blockquote_rule_and_hard_break() -> None:
    markdown = prosemirror_to_markdown(
        doc(
            {
                "type": "blockquote",
                "content": [
                    para(text("строка"), {"type": "hard_break"}, text("вторая")),
                    para(text("абзац")),
                ],
            },
            {"type": "horizontalRule"},
            para(text("после")),
        )
    )
    assert markdown == "> строка  \n> вторая\n>\n> абзац\n\n---\n\nпосле"


def test_table_with_header_escaped_pipes_and_ragged_rows() -> None:
    def cell(value: str, kind: str = "table_cell", **attrs: Any) -> dict[str, Any]:
        node: dict[str, Any] = {"type": kind, "content": [para(text(value))]}
        if attrs:
            node["attrs"] = attrs
        return node

    table = {
        "type": "table",
        "content": [
            {
                "type": "table_row",
                "content": [
                    cell("Должность", "table_header"),
                    cell("Дней", "tableHeader"),
                ],
            },
            {"type": "tableRow", "content": [cell("Инженер | старший"), cell("28")]},
            {"type": "table_row", "content": [cell("Стажёр", colspan=2)]},
            {
                "type": "table_row",
                "content": [
                    {
                        "type": "table_cell",
                        "content": [para(text("два")), para(text("абзаца"))],
                    },
                ],
            },
        ],
    }
    markdown = prosemirror_to_markdown(doc(table))
    assert markdown == (
        "| Должность | Дней |\n"
        "| --- | --- |\n"
        "| Инженер \\| старший | 28 |\n"
        "| Стажёр |  |\n"
        "| два абзаца |  |"
    )


def test_table_without_header_row_uses_first_row() -> None:
    table = {
        "type": "table",
        "content": [
            {
                "type": "table_row",
                "content": [{"type": "table_cell", "content": [para(text("а"))]}],
            },
        ],
    }
    assert prosemirror_to_markdown(doc(table)) == "| а |\n| --- |"


def test_image_becomes_its_alt_text_without_url() -> None:
    markdown = prosemirror_to_markdown(
        doc(
            para(
                text("Схема: "),
                {"type": "image", "attrs": {"src": "https://x/1.png", "alt": "план"}},
            )
        )
    )
    assert markdown == "Схема: план"


def test_unknown_nodes_become_plain_text() -> None:
    markdown = prosemirror_to_markdown(
        doc(
            {
                "type": "callout",
                "attrs": {"kind": "warn"},
                "content": [
                    para(text("Внимание", "strong")),
                    {"type": "mystery_inline", "content": [text("внутри")]},
                ],
            },
            para(text("Автор: "), {"type": "mention", "attrs": {"label": "Анна"}}),
            {"type": "embed", "attrs": {"src": "https://evil/"}},
        )
    )
    assert markdown == "**Внимание**\n\nвнутри\n\nАвтор: Анна"


def test_data_as_json_string_and_garbage() -> None:
    raw = json.dumps(doc(para(text("строкой"))))
    assert prosemirror_to_markdown(raw) == "строкой"
    assert prosemirror_to_markdown("не json") == ""
    assert prosemirror_to_markdown(None) == ""
    assert prosemirror_to_markdown([1, 2]) == ""
    assert prosemirror_to_markdown({"type": "doc", "content": [None, 5, "x"]}) == ""
    assert prosemirror_to_markdown(doc(para({"type": "text", "text": 7}))) == ""


def test_deep_nesting_is_cut_not_crashing() -> None:
    node: dict[str, Any] = para(text("дно"))
    for _ in range(MAX_DEPTH * 4):
        node = {"type": "blockquote", "content": [node]}
    markdown = prosemirror_to_markdown(doc(node))
    assert "дно" not in markdown
    assert len(markdown) < MAX_DEPTH * 8


def test_title_is_prepended_unless_document_starts_with_it() -> None:
    body = doc(para(text("текст")))
    assert prosemirror_to_markdown(body, title="Отпуск") == "# Отпуск\n\nтекст"
    same = doc(heading(1, text("Отпуск")), para(text("текст")))
    assert prosemirror_to_markdown(same, title="Отпуск") == "# Отпуск\n\nтекст"
