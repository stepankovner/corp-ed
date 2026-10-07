"""Общее для .xlsx и .pptx (ingest/ooxml.py): DTD и каталог архива.

DTD отклоняет сам разбор XML, а не поиск байтов `<!DOCTYPE` в начале
части: длинный комментарий перед DTD или часть в UTF-16 такой поиск не
видит, а разборщик — видит.

Число записей zip проверяется до того, как zipfile построит каталог.
"""

import io
import zipfile
from collections.abc import Callable

import pytest

from corp_ed.ingest import ooxml
from corp_ed.ingest.ooxml import OfficeFileError
from corp_ed.ingest.pptx import read_presentation
from corp_ed.ingest.xlsx import check_container, read_workbook, xlsx_to_markdown
from tests.ingest import samples
from tests.ingest.pptx_samples import SlideSpec, pptx, title
from tests.ingest.xlsx_samples import SheetSpec, xlsx

_DECLARATION = '<?xml version="1.0" encoding="UTF-8"?>'
_DTD = '<!DOCTYPE x [<!ENTITY a "aaaa">]>'
_LONG_COMMENT = "<!--" + "x" * 5000 + "-->"
_LONG_PI = "<?pad " + "x" * 5000 + "?>"

_Change = Callable[[str], bytes]


def _replace_part(data: bytes, part: str, change: _Change) -> bytes:
    source = zipfile.ZipFile(io.BytesIO(data))
    patched = io.BytesIO()
    with zipfile.ZipFile(patched, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in source.infolist():
            content = source.read(item)
            if item.filename == part:
                content = change(content.decode("utf-8"))
            archive.writestr(item, content)
    return patched.getvalue()


def _utf8(prolog: str) -> _Change:
    """Между объявлением XML и корнем — prolog."""

    def change(text: str) -> bytes:
        body = text.removeprefix(_DECLARATION)
        return (_DECLARATION + prolog + body).encode("utf-8")

    return change


def _utf16(prolog: str) -> _Change:
    """Та же часть в UTF-16 с BOM: ASCII-образец `<!DOCTYPE` в ней не найти."""

    def change(text: str) -> bytes:
        body = text.removeprefix(_DECLARATION)
        declaration = '<?xml version="1.0" encoding="UTF-16"?>'
        return (declaration + prolog + body).encode("utf-16")

    return change


def _workbook() -> bytes:
    return xlsx([SheetSpec("Лист1", {"A1": "Страна", "A2": "Франция"})])


@pytest.mark.parametrize(
    "change",
    [
        _utf8(_LONG_COMMENT + _DTD),
        _utf8(_LONG_PI + _DTD),
        _utf16(_DTD),
        _utf8(_DTD),
    ],
    ids=["after-long-comment", "after-long-pi", "utf-16", "direct"],
)
def test_doctype_in_workbook_part_is_corrupted(change: _Change) -> None:
    data = _replace_part(_workbook(), "xl/sharedStrings.xml", change)
    with pytest.raises(OfficeFileError) as error:
        read_workbook(data)
    assert error.value.code == "corrupted"


@pytest.mark.parametrize("part", ["[Content_Types].xml", "_rels/.rels"])
@pytest.mark.parametrize(
    "change",
    [_utf8(_LONG_COMMENT + _DTD), _utf16(_DTD)],
    ids=["after-long-comment", "utf-16"],
)
def test_container_check_rejects_doctype_in_package_parts(
    part: str, change: _Change
) -> None:
    """Эти части читает detect_format в процессе API, до песочницы."""
    data = _replace_part(_workbook(), part, change)
    with pytest.raises(OfficeFileError) as error:
        check_container(data)
    assert error.value.code == "corrupted"


def test_doctype_after_long_comment_in_slide_is_corrupted() -> None:
    data = _replace_part(
        pptx([SlideSpec([title("x")])]),
        "ppt/slides/slide1.xml",
        _utf8(_LONG_COMMENT + _DTD),
    )
    with pytest.raises(OfficeFileError) as error:
        read_presentation(data)
    assert error.value.code == "corrupted"


def test_part_in_utf16_without_doctype_is_read() -> None:
    """UTF-16 сам по себе не повод отклонять файл."""
    data = _replace_part(_workbook(), "xl/sharedStrings.xml", _utf16(""))
    assert "Франция" in xlsx_to_markdown(data)


def test_long_comment_without_doctype_is_read() -> None:
    data = _replace_part(_workbook(), "xl/sharedStrings.xml", _utf8(_LONG_COMMENT))
    assert "Франция" in xlsx_to_markdown(data)


# --- каталог архива -----------------------------------------------------------


def _forbid_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    """zipfile.ZipFile сразу строит ZipInfo на каждую запись: до него
    проверка дойти не должна."""

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("zip directory was built")

    monkeypatch.setattr(ooxml.zipfile, "ZipFile", fail)


def _code(data: bytes) -> str:
    with pytest.raises(OfficeFileError) as error:
        check_container(data)
    return error.value.code


def test_declared_entry_count_over_limit_is_rejected() -> None:
    data = samples.declare_entries(_workbook(), ooxml.MAX_ENTRIES + 1)
    assert _code(data) == "archive_too_large"


def test_too_many_entries_are_rejected_before_directory_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """zipfile на объявленное в EOCD число не смотрит, а читает весь
    каталог: заниженное число не должно пропускать архив."""
    data = samples.declare_entries(
        samples.with_entries(_workbook(), ooxml.MAX_ENTRIES), 3
    )
    _forbid_directory(monkeypatch)
    assert _code(data) == "archive_too_large"


def test_zip64_declared_entry_count_over_limit_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = samples.with_zip64(_workbook(), 10**6)
    _forbid_directory(monkeypatch)
    assert _code(data) == "archive_too_large"


def test_zip64_with_honest_entry_count_is_read() -> None:
    workbook = _workbook()
    count = len(zipfile.ZipFile(io.BytesIO(workbook)).infolist())
    data = samples.with_zip64(workbook, count)
    check_container(data)
    assert "Франция" in xlsx_to_markdown(data)


def test_zip64_locator_without_record_is_corrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = samples.with_zip64(_workbook(), 1, record=False)
    _forbid_directory(monkeypatch)
    assert _code(data) == "corrupted"
