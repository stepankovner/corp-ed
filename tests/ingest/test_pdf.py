"""ingest.pdf — разбор PDF без AGPL (pdfplumber / pdfminer.six), П-12."""

from pathlib import Path

import pytest

from corp_ed.ingest import pdf as pdf_module
from corp_ed.ingest.pdf import PdfError, pdf_to_markdown
from corp_ed.ingest.preprocess import PAGE_BREAK, preprocess
from tests.ingest.pdf_samples import Page, pdf

FIXTURES = Path(__file__).parent / "fixtures"


def _code(func, data: bytes) -> str:  # type: ignore[no-untyped-def]
    with pytest.raises(PdfError) as info:
        func(data)
    return info.value.code


def test_headings_by_size_and_bold_numbered_section() -> None:
    page = (
        Page()
        .text(72, 780, "Travel Policy", size=18, bold=True)
        .text(72, 740, "Allowances", size=14, bold=True)
        .text(72, 715, "Per diem is paid for every day of the trip.")
        .text(72, 690, "2. GENERAL RULES", bold=True)
        .text(72, 670, "Rules apply to all staff.")
    )

    markdown = pdf_to_markdown(pdf([page]))

    assert "# Travel Policy" in markdown
    assert "## Allowances" in markdown
    # Жирная строка с номером кеглем текста — раздел следующего уровня.
    assert "### 2. GENERAL RULES" in markdown
    assert "Per diem is paid for every day of the trip." in markdown


def test_grid_table_becomes_markdown_table_with_header() -> None:
    page = Page().table(
        72, 700, [150, 150], [["City", "Rate"], ["Moscow", "1200"], ["Other", "800"]]
    )

    markdown = pdf_to_markdown(pdf([page]))

    assert "|City|Rate|\n|---|---|\n|Moscow|1200|\n|Other|800|" in markdown
    assert "City: Moscow; Rate: 1200" in preprocess(markdown)


def test_superscript_footnote_mark_is_tagged() -> None:
    # Helvetica 11 пт: «Budget is 5 000 000» кончается на x ≈ 169,8.
    page = Page().text(72, 700, "Budget is 5 000 000").text(170, 704, "1", size=6)

    markdown = pdf_to_markdown(pdf([page]))

    assert "5 000 000<sup>1</sup>" in markdown
    assert "5 000 0001" not in preprocess(markdown)


def test_table_continued_on_next_page_gets_header_of_first_part() -> None:
    first = Page().table(
        72, 300, [60, 200], [["No", "Project"], ["1", "Alpha"], ["2", "Beta"]]
    )
    second = (
        Page()
        .text(290, 30, "2")
        .table(72, 800, [60, 200], [["3", "Gamma"], ["4", "Delta"]])
    )

    pages = pdf_to_markdown(pdf([first, second])).split(PAGE_BREAK)

    assert pages[1].startswith("|No|Project|\n|---|---|\n|3|Gamma|")
    assert "No: 3; Project: Gamma" in preprocess(PAGE_BREAK.join(pages))


def test_two_columns_are_read_column_by_column() -> None:
    page = Page()
    left = ["Left column one", "left column two", "left column three"]
    right = ["Right column one", "right column two", "right column three"]
    for i, (a, b) in enumerate(zip(left, right, strict=True)):
        page.text(72, 700 - 14 * i, a).text(330, 700 - 14 * i, b)

    markdown = " ".join(pdf_to_markdown(pdf([page])).split())

    assert markdown.index("left column three") < markdown.index("Right column one")


def test_container_errors() -> None:
    assert _code(pdf_to_markdown, b"PK\x03\x04 not a pdf") == "format_mismatch"
    assert _code(pdf_to_markdown, b"%PDF-1.4\n garbage") == "corrupted"


def test_password_protected_pdf_is_encrypted() -> None:
    data = (FIXTURES / "encrypted.pdf").read_bytes()

    assert _code(pdf_to_markdown, data) == "encrypted"


def test_owner_password_only_pdf_is_read() -> None:
    # Пароль только на права (печать, правка) — читается, как у pymupdf.
    data = (FIXTURES / "owner_password.pdf").read_bytes()

    assert pdf_to_markdown(data) == "Owner only"


def test_too_many_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_module, "MAX_PAGES", 2)
    data = pdf([Page().text(72, 700, "x") for _ in range(3)])

    assert _code(pdf_to_markdown, data) == "too_many_pages"


# --- правила, которые латиницей стандартных шрифтов не проверить ---------------


def _block(*lines: tuple[str, float, bool]) -> pdf_module._Block:
    block = pdf_module._Block(top=0.0)
    for text, size, bold in lines:
        letters = sum(ch.isalpha() for ch in text)
        block.lines.append(pdf_module._Line(text, size, bold, letters))
    return block


def test_cover_place_and_year_is_not_a_heading() -> None:
    # «г. Москва» и «2026 год» — две строки обложки одного кегля.
    blocks = [_block(("г. Москва", 14.0, False), ("2026 год", 14.0, False))]

    markdown = pdf_module._render(blocks, [14.0], cover=False)

    assert markdown == "г. Москва 2026 год"


def test_cover_page_large_lines_are_not_headings() -> None:
    blocks = [
        _block(("ПОЛОЖЕНИЕ о конкурсе", 14.0, True)),
        _block(("1. ОБЩИЕ ПОЛОЖЕНИЯ", 14.0, True)),
    ]

    markdown = pdf_module._render(blocks, [14.0], cover=True)

    assert not markdown.startswith("# ПОЛОЖЕНИЕ")
    assert "# 1. ОБЩИЕ ПОЛОЖЕНИЯ" in markdown


def test_numbered_lead_in_before_list_is_subheading() -> None:
    blocks = [
        _block(("II. Взаимодействие Сторон", 12.0, True)),
        _block(("2.1. Предприятие обязуется:", 12.0, False)),
        _block(("2.1.1. представить отчёт;", 12.0, False)),
    ]

    lines = pdf_module._render(blocks, [], cover=False).split("\n\n")

    assert lines[0] == "# II. Взаимодействие Сторон"
    assert lines[1] == "## 2.1. Предприятие обязуется:"


def test_long_preamble_level_used_only_at_start_is_dropped() -> None:
    preamble = _block(
        ("Приложение № 1 к Документации", 14.0, True),
        *[
            (f"Информационные ресурсы федеральных органов, часть {n}", 14.0, True)
            for n in range(1, 5)
        ],
    )
    sections = [_block((f"{n}. ЦКР «Раздел {n}»", 12.0, True)) for n in (1, 2)]
    body = [_block(("текст раздела", 11.0, False))]

    levels = pdf_module._drop_preamble_levels([[preamble, *sections, *body]], [14.0])

    assert levels == []


def test_short_document_title_level_is_kept() -> None:
    title = _block(
        ("Положение о служебных", 26.0, False),
        ("командировках (редакция", 26.0, False),
        ("2026 года)", 26.0, False),
    )
    sections = [_block((f"{n}. Раздел {n}", 14.0, True)) for n in (1, 2)]
    body = [_block(("текст раздела", 12.0, False))]

    levels = pdf_module._drop_preamble_levels([[title, *sections, *body]], [26.0, 14.0])

    assert levels == [26.0, 14.0]


def test_line_join_keeps_compound_words_and_codes() -> None:
    assert pdf_module._join("научно-", "технической") == "научно-технической"
    assert pdf_module._join("С1ИИ-", "602444") == "С1ИИ-602444"
    assert pdf_module._join("Москва", "Санкт-Петербург") == "Москва Санкт-Петербург"
