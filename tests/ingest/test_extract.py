"""Извлечение текста из файлов: форматы, подмены, бомбы, кодировки."""

import io
import sys
import time
import zipfile
from pathlib import Path

import pytest

from corp_ed.core.config import IngestSettings
from corp_ed.ingest import extract as extract_module
from corp_ed.ingest import pdf as pdf_module
from corp_ed.ingest import sandbox
from corp_ed.ingest.extract import (
    ExtractionError,
    SourceFormat,
    detect_format,
    error_message,
    extract,
    max_file_bytes,
)
from corp_ed.ingest.preprocess import PAGE_BREAK, preprocess
from corp_ed.ingest.sandbox import _clean_env, _crash_code, cpu_budget, extract_isolated
from tests.ingest import samples

FIXTURES = Path(__file__).parent / "fixtures"


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


@pytest.mark.parametrize("filename", ["old.rtf", "slides.ppt", "run.exe", "noext"])
def test_rejects_unsupported_extension(filename: str) -> None:
    assert _code(lambda: detect_format(filename, b"data")) == "unsupported_format"


@pytest.mark.parametrize(
    ("filename", "advice"),
    [
        ("old.rtf", ".docx или PDF"),
        ("slides.odp", ".pptx или PDF"),
        ("budget.ods", ".xlsx или PDF"),
    ],
)
def test_unsupported_format_message_gives_advice(filename: str, advice: str) -> None:
    """Решение 28.09 (П-3): совет, как загрузить файл, а не только отказ.
    Совет по флагу форматов Р-5 — tests/ingest/test_office_intake.py."""
    message = error_message("unsupported_format", filename)
    assert advice in message
    assert message.endswith("Поддерживаются файлы docx, doc, xlsx, pptx, pdf, txt и md")


def test_unknown_format_keeps_plain_message() -> None:
    assert error_message("unsupported_format", "run.exe") == (
        "Поддерживаются файлы docx, doc, xlsx, pptx, pdf, txt и md"
    )


def test_rejects_pdf_renamed_to_docx() -> None:
    assert _code(lambda: detect_format("a.docx", b"%PDF-1.7")) == "format_mismatch"


def test_rejects_docx_renamed_to_pdf() -> None:
    docx = samples.docx([("Текст", None)])
    assert _code(lambda: detect_format("a.pdf", docx)) == "format_mismatch"


@pytest.mark.parametrize(
    "magic", [b"MZ\x90\x00", b"\x7fELF\x02", b"PK\x03\x04", b"\xd0\xcf\x11\xe0"]
)
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


def test_rejects_docx_declaring_too_many_entries() -> None:
    data = samples.declare_entries(
        samples.docx([("Текст", None)]), extract_module.MAX_DOCX_ENTRIES + 1
    )
    assert _code(lambda: detect_format("a.docx", data)) == "archive_too_large"


def test_docx_entries_are_counted_before_directory_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Каталог zip в процессе API не строится, если записей больше
    лимита, даже когда конец архива объявляет их меньше."""
    data = samples.declare_entries(
        samples.with_entries(
            samples.docx([("Текст", None)]), extract_module.MAX_DOCX_ENTRIES
        ),
        3,
    )

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("zip directory was built")

    monkeypatch.setattr(extract_module.zipfile, "ZipFile", fail)
    assert _code(lambda: detect_format("a.docx", data)) == "archive_too_large"


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


def test_docx_superscript_does_not_stick_to_numbers() -> None:
    """BH-36: верхний индекс, набранный вручную вместо сноски, mammoth отдаёт
    тегом <sup>; без тега цифра прилипала к числу — «5 000 0001»."""
    sup = '<w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:t>{}</w:t></w:r>'
    text = '<w:r><w:t xml:space="preserve">{}</w:t></w:r>'
    body = (
        "<w:p>"
        + text.format("Бюджет проекта 5 000 000")
        + sup.format("1")
        + text.format(" ₽.")
        + "</w:p><w:p>"
        + text.format("Площадь склада 120 м")
        + sup.format("2")
        + text.format(".")
        + "</w:p>"
    )
    markdown = preprocess(extract(SourceFormat.DOCX, samples.docx([], body_xml=body)))

    assert "Бюджет проекта 5 000 000 ₽." in markdown
    assert "5 000 0001" not in markdown
    assert "Площадь склада 120 м²." in markdown


def test_docx_running_text_is_extracted() -> None:
    """Стенд 02.10: номер положения жил только в верхнем колонтитуле."""
    data = samples.docx(
        [("Положение об отпусках", "Heading1"), ("Текст положения.", None)],
        extra_parts={
            "word/header1.xml": samples.running_part("hdr", ["Положение П-ОТП-07"]),
            "word/header2.xml": samples.running_part("hdr", ["Положение П-ОТП-07"]),
            "word/footer1.xml": samples.running_part(
                "ftr", ["Вопросы — внутренний телефон 2318", "7"]
            ),
        },
    )
    markdown = extract(SourceFormat.DOCX, data)

    assert markdown.index("П-ОТП-07") < markdown.index("# Положение об отпусках")
    assert markdown.count("П-ОТП-07") == 1
    assert markdown.rstrip().endswith("Вопросы — внутренний телефон 2318")
    assert "\n7" not in markdown


_DOCTYPE = '<!DOCTYPE w:document [<!ENTITY a "aaaa">]>'


@pytest.mark.parametrize("part", ["word/document.xml", "word/styles.xml"])
@pytest.mark.parametrize(
    "prolog",
    [_DOCTYPE, "<!--" + "x" * 5000 + "-->" + _DOCTYPE],
    ids=["direct", "padded"],
)
def test_docx_part_with_doctype_is_corrupted(part: str, prolog: str) -> None:
    """DTD Word не пишет: часть с ним — повреждённый файл, как в xlsx и
    pptx. mammoth разбирает части через minidom и сам DTD не запрещает."""
    data = samples.docx([("Текст положения.", None)])
    source = zipfile.ZipFile(io.BytesIO(data))
    patched = io.BytesIO()
    with zipfile.ZipFile(patched, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in source.infolist():
            content = source.read(item)
            if item.filename == part:
                declaration, rest = content.split(b"?>", 1)
                content = declaration + b"?>" + prolog.encode() + rest.lstrip()
            archive.writestr(item, content)
    assert _code(lambda: extract(SourceFormat.DOCX, patched.getvalue())) == (
        "corrupted"
    )


def test_docx_with_images_and_binary_parts_is_read() -> None:
    """Предпроверка DTD не спотыкается о части, которые не XML."""
    data = samples.docx(
        [("Текст положения.", None)],
        extra_parts={"word/media/image1.png": "\x89PNG\r\n\x1a\n\x00\x00binary"},
    )
    assert "Текст положения." in extract(SourceFormat.DOCX, data)


def test_docx_with_unreadable_unused_part_is_read() -> None:
    """Часть, которую zipfile не открывает (помечена зашифрованной), а
    mammoth не читает, документ не валит — как и до предпроверки DTD."""
    data = bytearray(
        samples.docx(
            [("Текст положения.", None)],
            extra_parts={"word/media/locked.bin": "data"},
        )
    )
    entry = data.rfind(b"PK\x01\x02", 0, data.rfind(b"word/media/locked.bin"))
    data[entry + 8] |= 0x01  # флаг «зашифровано» в центральном каталоге
    assert "Текст положения." in extract(SourceFormat.DOCX, bytes(data))


def test_broken_running_text_does_not_fail_docx() -> None:
    data = samples.docx(
        [("Текст положения.", None)],
        extra_parts={"word/header1.xml": "<w:hdr"},
    )
    assert "Текст положения." in extract(SourceFormat.DOCX, data)


def test_pdf_pages_are_joined_with_page_break() -> None:
    data = samples.pdf([[("First page text", 11)], [("Second page text", 11)]])
    markdown = extract(SourceFormat.PDF, data)

    assert markdown.count(PAGE_BREAK) == 1
    assert "First page text" in markdown and "Second page text" in markdown


def test_encrypted_pdf_is_rejected() -> None:
    data = (FIXTURES / "encrypted.pdf").read_bytes()
    assert _code(lambda: extract(SourceFormat.PDF, data)) == "encrypted"


def test_pdf_without_text_is_rejected() -> None:
    """Скан без текстового слоя: сообщение, а не пустой материал."""
    data = samples.pdf([[]])
    assert _code(lambda: extract(SourceFormat.PDF, data)) == "no_text"


def test_corrupted_pdf_is_rejected() -> None:
    assert _code(lambda: extract(SourceFormat.PDF, b"%PDF-1.7 broken")) == ("corrupted")


def test_windows_encodings_are_read() -> None:
    """Стенд 02.10: .txt из старого Блокнота отклонялся."""
    text = "Отпуск — 28 календарных дней.\r\nПропуск: кабинет 101."
    assert extract(SourceFormat.TXT, text.encode("cp1251")) == text
    assert extract(SourceFormat.TXT, text.encode("utf-16")) == text
    big_endian = b"\xfe\xff" + text.encode("utf-16-be")
    assert extract(SourceFormat.MD, big_endian) == text


@pytest.mark.parametrize(
    "data",
    [
        bytes(range(0x80, 0x98)) * 50,  # не текст ни в одной кодировке
        # Западноевропейский текст: в cp1251 акценты стали бы кириллицей.
        "Le café du coin est fermé le dimanche. Über alles.".encode("latin-1"),
        b"\x01\x02\x03\xe0\xe1" * 200,  # двоичный файл
    ],
)
def test_undecodable_text_is_rejected(data: bytes) -> None:
    assert _code(lambda: extract(SourceFormat.TXT, data)) == "not_utf8"


def test_bom_and_nul_are_removed() -> None:
    data = "\ufeffСтрока\x00 текста".encode()
    assert extract(SourceFormat.TXT, data) == "Строка текста"


def test_empty_text_is_rejected() -> None:
    assert _code(lambda: extract(SourceFormat.TXT, b"  \n\n ")) == "no_text"
    assert error_message("no_text", "пусто.txt") == "Файл пустой — в нём нет текста"
    assert "скан" in error_message("no_text", "скан.pdf")


def test_oversized_text_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract_module, "MAX_EXTRACTED_CHARS", 10)
    assert _code(lambda: extract(SourceFormat.TXT, b"x" * 11)) == ("document_too_large")


def test_too_many_pdf_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_module, "MAX_PAGES", 1)
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


async def test_sandbox_parses_pdf_with_headings() -> None:
    """BH-39: разбор ML (pdfplumber) в песочнице — заголовок по кеглю."""
    data = samples.pdf([[("Vacation policy", 18), ("Vacation lasts 28 days.", 11)]])
    markdown = await extract_isolated(SourceFormat.PDF, data)
    assert "# Vacation policy" in markdown
    assert "28 days" in markdown


def test_old_pdf_layout_setting_does_not_break_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INGEST_PDF_LAYOUT в старом .env сервера — не ошибка: модели больше нет."""
    monkeypatch.setenv("INGEST_PDF_LAYOUT", "false")
    assert IngestSettings().extra_format_names


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


def _fake_worker(monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    """Вместо extract_worker — дочерний процесс с заданным поведением."""
    monkeypatch.setattr(
        sandbox,
        "_worker_command",
        lambda fmt, cpu_seconds: [sys.executable, "-I", "-c", script],
    )


async def test_sandbox_kills_child_that_floods_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сломанный дочерний процесс не забивает память API своим выводом:
    после потолка его убивают, не дожидаясь таймаута."""
    monkeypatch.setattr(sandbox, "MAX_OUTPUT_BYTES", 1024 * 1024)
    _fake_worker(
        monkeypatch,
        "import sys, time\n"
        "sys.stdout.buffer.write(b'x' * (2 * 1024 * 1024))\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)\n",
    )
    started = time.monotonic()
    with pytest.raises(ExtractionError) as info:
        await extract_isolated(SourceFormat.TXT, b"text", timeout=10)
    assert info.value.code == "document_too_large"
    assert time.monotonic() - started < 5


async def test_sandbox_reads_answer_after_long_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Длинный stderr не мешает прочитать ответ; дочерний процесс, который
    не дочитал stdin, — тоже."""
    _fake_worker(
        monkeypatch,
        "import json, sys\n"
        "sys.stderr.buffer.write(b'e' * (3 * 1024 * 1024))\n"
        "sys.stdout.write(json.dumps({'ok': True, 'markdown': 'готово'}))\n",
    )
    data = b"x" * (4 * 1024 * 1024)
    assert await extract_isolated(SourceFormat.TXT, data, timeout=30) == "готово"


def test_sandbox_environment_has_no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Процесс, читающий враждебный файл, не получает ключей API и базы."""
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("YC_API_KEY", "AQVN-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")

    env = _clean_env()

    assert set(env) == {"PATH", "LANG"}


@pytest.mark.parametrize(
    ("filename", "limit"),
    [
        ("Презентация.PPTX", 100),
        ("отчёт.pdf", 100),
        ("Положение.docx", 100),
        ("C:\\Users\\a\\deck.pptx", 100),
        ("таблица.xlsx", 25),
        ("старый.doc", 25),
        ("заметки.txt", 25),
        ("readme.md", 25),
        ("без_расширения", 25),
        ("archive.pdf.exe", 25),
    ],
)
def test_large_limit_only_for_pdf_docx_pptx(filename: str, limit: int) -> None:
    assert max_file_bytes(filename, large=100, other=25) == limit
