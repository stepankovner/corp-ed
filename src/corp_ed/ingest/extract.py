"""Извлечение текста из загруженных файлов → Markdown (BH-2).

Выбор библиотек — рекомендация ML по замеру на 6 реальных документах:
- docx: mammoth → HTML → markdownify (заголовки → #, таблицы, списки);
- pdf: pymupdf4llm (заголовки и таблицы; markitdown даёт ноль
  заголовков), страницы склеиваются PAGE_BREAK (\\f) — по ним preprocess
  находит колонтитулы;
- txt, md: как есть, только UTF-8.

Файл пришёл от клиента и считается враждебным. Здесь — проверки,
которые дешёво сделать до парсера: формат по сигнатуре, а не по
расширению; zip-бомба в docx; зашифрованный PDF; число страниц.
Сам разбор запускается в отдельном процессе с лимитами
(ingest/sandbox.py), этот модуль не вызывается из API напрямую.

Лицензии: pymupdf и pymupdf4llm — AGPL-3.0 (решение команды 25.09,
риск записан в RISKS.md). mammoth — BSD-2, markdownify — MIT.
"""

import io
import zipfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePath

from corp_ed.ingest.preprocess import PAGE_BREAK


class SourceFormat(StrEnum):
    DOCX = "docx"
    PDF = "pdf"
    TXT = "txt"
    MD = "md"


class ExtractionError(Exception):
    """Файл нельзя принять. code — для клиента и журнала, без деталей парсера."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# Сообщения для админа компании. Досье (3.3): неподдерживаемое
# отклоняется при загрузке с понятным сообщением.
ERROR_MESSAGES = {
    "unsupported_format": "Поддерживаются файлы docx, pdf, txt и md",
    "format_mismatch": "Содержимое файла не соответствует его расширению",
    "not_utf8": "Текстовый файл должен быть в кодировке UTF-8",
    "encrypted": "Файл защищён паролем — снимите защиту и загрузите снова",
    "no_text": "В файле нет текста (возможно, это скан без распознавания)",
    "too_many_pages": "Слишком много страниц в документе",
    "archive_too_large": "Файл распаковывается в слишком большой объём",
    "document_too_large": "Документ слишком большой",
    "corrupted": "Файл повреждён или не читается",
    "timeout": "Файл обрабатывается слишком долго",
}

MAX_PDF_PAGES = 1000
# docx — zip. Лимиты на распакованный объём и число файлов закрывают
# zip-бомбу: 40 КБ архива, которые распаковываются в гигабайты.
MAX_DOCX_UNCOMPRESSED = 200 * 1024 * 1024
MAX_DOCX_ENTRIES = 5000
MAX_COMPRESSION_RATIO = 200
MAX_EXTRACTED_CHARS = 2_000_000
"""~1700 чанков по 400 токенов: верхняя граница стоимости эмбеддингов
одного документа и размера строки в materials."""

_EXTENSIONS = {
    ".docx": SourceFormat.DOCX,
    ".pdf": SourceFormat.PDF,
    ".txt": SourceFormat.TXT,
    ".md": SourceFormat.MD,
    ".markdown": SourceFormat.MD,
}


@dataclass(frozen=True)
class DetectedFile:
    format: SourceFormat
    filename: str


def detect_format(filename: str, data: bytes) -> DetectedFile:
    """Формат по расширению, подтверждённый сигнатурой содержимого.

    Расширение — только заявка клиента. PDF, переименованный в .docx, или
    исполняемый файл, переименованный в .txt, отклоняются здесь, до
    парсера. Имя файла очищается от пути: оно хранится для справки и
    нигде не используется как путь.
    """
    name = PurePath(filename.replace("\\", "/")).name[:255] or "file"
    fmt = _EXTENSIONS.get(PurePath(name).suffix.lower())
    if fmt is None:
        raise ExtractionError("unsupported_format")

    if fmt is SourceFormat.PDF and not data.startswith(b"%PDF-"):
        raise ExtractionError("format_mismatch")
    if fmt is SourceFormat.DOCX:
        _check_docx_container(data)
    if fmt in (SourceFormat.TXT, SourceFormat.MD) and data.startswith(
        (b"%PDF-", b"PK\x03\x04", b"\x7fELF", b"MZ")
    ):
        raise ExtractionError("format_mismatch")
    return DetectedFile(format=fmt, filename=name)


def _check_docx_container(data: bytes) -> None:
    if not data.startswith(b"PK\x03\x04"):
        raise ExtractionError("format_mismatch")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
    except zipfile.BadZipFile as exc:
        raise ExtractionError("corrupted") from exc

    names = {entry.filename for entry in entries}
    if "[Content_Types].xml" not in names or "word/document.xml" not in names:
        # zip, но не документ Word (xlsx, pptx, просто архив).
        raise ExtractionError("format_mismatch")
    if len(entries) > MAX_DOCX_ENTRIES:
        raise ExtractionError("archive_too_large")

    total = 0
    for entry in entries:
        total += entry.file_size
        ratio = entry.file_size / max(entry.compress_size, 1)
        if total > MAX_DOCX_UNCOMPRESSED or ratio > MAX_COMPRESSION_RATIO:
            raise ExtractionError("archive_too_large")


def extract(fmt: SourceFormat, data: bytes) -> str:
    """Файл → Markdown до preprocess. Вызывать в песочнице (sandbox.py)."""
    if fmt is SourceFormat.DOCX:
        markdown = _extract_docx(data)
    elif fmt is SourceFormat.PDF:
        markdown = _extract_pdf(data)
    else:
        markdown = _decode_text(data)

    # NUL в тексте PostgreSQL не хранит, а в документах он встречается
    # после кривых конвертеров.
    markdown = markdown.replace("\x00", "")
    if not markdown.strip():
        raise ExtractionError("no_text")
    if len(markdown) > MAX_EXTRACTED_CHARS:
        raise ExtractionError("document_too_large")
    return markdown


def _decode_text(data: bytes) -> str:
    try:
        # utf-8-sig: BOM от Блокнота Windows не должен попасть в текст.
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ExtractionError("not_utf8") from exc


def _extract_docx(data: bytes) -> str:
    import mammoth  # type: ignore[import-untyped]
    import markdownify

    try:
        # Картинки не нужны: в текст они не превращаются, а base64 в
        # Markdown — мегабайты мусора в чанках.
        result = mammoth.convert_to_html(
            io.BytesIO(data),
            convert_image=mammoth.images.img_element(lambda image: {}),
        )
    except Exception as exc:  # noqa: BLE001 — любая ошибка парсера = битый файл
        raise ExtractionError("corrupted") from exc
    markdown: str = markdownify.markdownify(result.value, heading_style="ATX")
    return markdown


def _extract_pdf(data: bytes) -> str:
    import pymupdf
    import pymupdf4llm  # type: ignore[import-untyped]

    try:
        document = pymupdf.open(stream=data, filetype="pdf")  # type: ignore[no-untyped-call]
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError("corrupted") from exc

    with document:
        if document.needs_pass or document.is_encrypted:
            raise ExtractionError("encrypted")
        if document.page_count > MAX_PDF_PAGES:
            raise ExtractionError("too_many_pages")
        try:
            pages = pymupdf4llm.to_markdown(
                document, page_chunks=True, show_progress=False
            )
        except Exception as exc:  # noqa: BLE001
            raise ExtractionError("corrupted") from exc

    # Договорённость с ML: страницы склеиваются \f — по границам страниц
    # preprocess находит и удаляет колонтитулы.
    return PAGE_BREAK.join(str(page["text"]) for page in pages)
