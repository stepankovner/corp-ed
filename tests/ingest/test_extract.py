"""Извлечение текста из файлов: форматы, подмены, бомбы, кодировки."""

import io
import zipfile

import pytest

from corp_ed.ingest import extract as extract_module
from corp_ed.ingest.extract import (
    ExtractionError,
    SourceFormat,
    detect_format,
    extract,
)
from corp_ed.ingest.preprocess import PAGE_BREAK
from corp_ed.ingest.sandbox import _clean_env, _crash_code, cpu_budget, extract_isolated
from tests.ingest import samples


def _code(call: object) -> str:
    with pytest.raises(ExtractionError) as info:
        call()  # type: ignore[operator]
    return info.value.code


# --- формат ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "fmt"),
    [
        ("Положение.docx", SourceFormat.DOCX),
        ("REGLAMENT.PDF", SourceFormat.PDF),
        ("notes.txt", SourceFormat.TXT),
        ("readme.md", SourceFormat.MD),
    ],
)
def test_detects_supported_formats(filename: str, fmt: SourceFormat) -> None:
    data = {
        SourceFormat.DOCX: samples.docx([("Текст", None)]),
        SourceFormat.PDF: b"%PDF-1.7\n",
        SourceFormat.TXT: "текст".encode(),
        SourceFormat.MD: b"# Title",
    }[fmt]
    assert detect_format(filename, data).format is fmt


@pytest.mark.parametrize("filename", ["old.doc", "slides.pptx", "run.exe", "noext"])
def test_rejects_unsupported_extension(filename: str) -> None:
    assert _code(lambda: detect_format(filename, b"data")) == "unsupported_format"


def test_rejects_pdf_renamed_to_docx() -> None:
    assert _code(lambda: detect_format("a.docx", b"%PDF-1.7")) == "format_mismatch"


def test_rejects_docx_renamed_to_pdf() -> None:
    docx = samples.docx([("Текст", None)])
    assert _code(lambda: detect_format("a.pdf", docx)) == "format_mismatch"


@pytest.mark.parametrize("magic", [b"MZ\x90\x00", b"\x7fELF\x02", b"PK\x03\x04"])
def test_rejects_binary_renamed_to_text(magic: bytes) -> None:
    assert _code(lambda: detect_format("a.txt", magic + b"rest")) == "format_mismatch"


def test_rejects_zip_that_is_not_word() -> None:
    """xlsx или просто архив с расширением .docx."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", "<a/>")
    assert _code(lambda: detect_format("a.docx", buffer.getvalue())) == (
        "format_mismatch"
    )


def test_rejects_zip_bomb() -> None:
    bomb = samples.zip_bomb_docx()
    assert len(bomb) < 1024 * 1024
    assert _code(lambda: detect_format("a.docx", bomb)) == "archive_too_large"


def test_rejects_broken_zip() -> None:
    assert _code(lambda: detect_format("a.docx", b"PK\x03\x04garbage")) == "corrupted"


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("../../etc/passwd.txt", "passwd.txt"),
        ("C:\\Users\\boss\\secret.txt", "secret.txt"),
        ("/abs/path/file.md", "file.md"),
    ],
)
def test_filename_is_stripped_of_path(raw: str, clean: str) -> None:
    """Имя хранится для справки и никогда не используется как путь."""
    assert detect_format(raw, b"text").filename == clean


# --- содержимое ---------------------------------------------------------------


def test_docx_headings_become_markdown() -> None:
    data = samples.docx(
        [
            ("Положение об отпусках", "Heading1"),
            ("3.2 Перенос отпуска", "Heading2"),
            ("По заявлению работника.", None),
        ]
    )
    markdown = extract(SourceFormat.DOCX, data)

    assert "# Положение об отпусках" in markdown
    assert "## 3.2 Перенос отпуска" in markdown
    assert "По заявлению работника." in markdown


def test_pdf_pages_are_joined_with_page_break() -> None:
    data = samples.pdf([[("First page text", 11)], [("Second page text", 11)]])
    markdown = extract(SourceFormat.PDF, data)

    assert markdown.count(PAGE_BREAK) == 1
    assert "First page text" in markdown and "Second page text" in markdown


def test_encrypted_pdf_is_rejected() -> None:
    data = samples.pdf([[("Secret", 11)]], password="pw")
    assert _code(lambda: extract(SourceFormat.PDF, data)) == "encrypted"


def test_pdf_without_text_is_rejected() -> None:
    """Скан без текстового слоя: сообщение, а не пустой материал."""
    data = samples.pdf([[]])
    assert _code(lambda: extract(SourceFormat.PDF, data)) == "no_text"


def test_corrupted_pdf_is_rejected() -> None:
    assert _code(lambda: extract(SourceFormat.PDF, b"%PDF-1.7 broken")) == ("corrupted")


def test_text_must_be_utf8() -> None:
    cp1251 = "Отпуск 28 дней".encode("cp1251")
    assert _code(lambda: extract(SourceFormat.TXT, cp1251)) == "not_utf8"


def test_bom_and_nul_are_removed() -> None:
    data = "\ufeffСтрока\x00 текста".encode()
    assert extract(SourceFormat.TXT, data) == "Строка текста"


def test_empty_text_is_rejected() -> None:
    assert _code(lambda: extract(SourceFormat.TXT, b"  \n\n ")) == "no_text"


def test_oversized_text_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract_module, "MAX_EXTRACTED_CHARS", 10)
    assert _code(lambda: extract(SourceFormat.TXT, b"x" * 11)) == ("document_too_large")


def test_too_many_pdf_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract_module, "MAX_PDF_PAGES", 1)
    data = samples.pdf([[("a", 11)], [("b", 11)]])
    assert _code(lambda: extract(SourceFormat.PDF, data)) == "too_many_pages"


# --- песочница ----------------------------------------------------------------


async def test_sandbox_extracts_in_child_process() -> None:
    data = samples.docx([("Раздел", "Heading1"), ("Текст раздела.", None)])
    markdown = await extract_isolated(SourceFormat.DOCX, data)
    assert "# Раздел" in markdown


async def test_sandbox_passes_error_codes() -> None:
    with pytest.raises(ExtractionError) as info:
        await extract_isolated(SourceFormat.PDF, b"%PDF-1.7 broken")
    assert info.value.code == "corrupted"


async def test_sandbox_times_out() -> None:
    data = samples.pdf([[("text", 11)]])
    with pytest.raises(ExtractionError) as info:
        await extract_isolated(SourceFormat.PDF, data, timeout=0.001)
    assert info.value.code == "timeout"


def test_kill_by_cpu_limit_is_reported_as_timeout() -> None:
    """SIGXCPU/SIGKILL от лимита — «слишком долго», падение парсера —
    «повреждён»."""
    assert _crash_code(-9) == "timeout"
    assert _crash_code(-24) == "timeout"
    assert _crash_code(-11) == "corrupted"
    assert _crash_code(1) == "corrupted"


def test_cpu_budget_covers_all_cores_for_the_whole_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("corp_ed.ingest.sandbox.os.cpu_count", lambda: 4)
    assert cpu_budget(90.0) == 360
    assert cpu_budget(0.001) == 4
    monkeypatch.setattr("corp_ed.ingest.sandbox.os.cpu_count", lambda: None)
    assert cpu_budget(10) == 10


async def test_sandbox_accepts_explicit_cpu_budget() -> None:
    data = samples.docx([("Раздел", "Heading1"), ("Текст.", None)])
    markdown = await extract_isolated(SourceFormat.DOCX, data, cpu_seconds=120)
    assert "# Раздел" in markdown


def test_sandbox_environment_has_no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Процесс, читающий враждебный файл, не получает ключей API и базы."""
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("YC_API_KEY", "AQVN-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")

    env = _clean_env()

    assert set(env) == {"PATH", "LANG"}
