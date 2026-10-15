"""Генератор корпуса: каждый документ во всех форматах читается тем же
разбором, что и в продукте, и шифр с правкой доходят до текста."""

import pytest

from corp_ed.ingest.extract import detect_format, extract
from tests.acceptance.corpus.build import (
    FORMATS,
    SUFFIX,
    parse,
    read_source,
    render,
    staff_rows,
    staff_xlsx,
)
from tests.acceptance.model import load_manifest
from tests.acceptance.scenario import CODE

MANIFEST = load_manifest()
EDIT = "ПРАВКА-9: проверочная строка."


def _extracted(fmt: str, data: bytes) -> str:
    detected = detect_format(f"x{SUFFIX[fmt]}", data)
    return extract(detected.format, data)


@pytest.mark.parametrize(
    "document", list(MANIFEST.documents.values()), ids=lambda d: d.id
)
def test_document_survives_its_own_format(document) -> None:  # type: ignore[no-untyped-def]
    data = render(read_source(document.file), document.format, [EDIT])
    text = _extracted(document.format, data)
    found = CODE.search(text)
    assert found is not None and found.group(1) == document.id
    assert "ПРАВКА-9" in text
    # Кириллица не потерялась: строка шапки на месте.
    assert "Владелец" in text


@pytest.mark.parametrize("fmt", FORMATS)
@pytest.mark.parametrize("doc_id", ["SV-SAL-001", "SV-GEN-015", "SV-PAY-001"])
def test_any_format_of_any_document(fmt: str, doc_id: str) -> None:
    document = MANIFEST.documents[doc_id]
    text = _extracted(fmt, render(read_source(document.file), fmt, [EDIT]))
    assert CODE.search(text) is not None
    assert "ПРАВКА-9" in text


def test_table_cells_reach_the_text() -> None:
    document = MANIFEST.documents["SV-SAL-001"]
    for fmt in ("docx", "xlsx", "pdf", "md"):
        text = _extracted(fmt, render(read_source(document.file), fmt))
        assert "189 000" in text, fmt


def test_parse_understands_the_sources() -> None:
    blocks = parse(
        "# Заголовок\n\n## Раздел\n\n- пункт\n1. шаг\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n\nТекст **жирный**."
    )
    kinds = [b.kind for b in blocks]
    assert kinds == ["h1", "h2", "li", "li", "table", "p"]
    assert blocks[4].rows == [["a", "b"], ["1", "2"]]
    assert blocks[5].text == "Текст жирный."


def test_unknown_format_is_rejected() -> None:
    with pytest.raises(ValueError):
        render("# x", "odt")


def test_staff_directory_is_stable_and_has_120_people() -> None:
    rows = staff_rows()
    assert len(rows) == 121
    assert rows == staff_rows()
    assert all(r[3].endswith("@severny-veter.test") for r in rows[1:])
    assert staff_xlsx()[:2] == b"PK"
