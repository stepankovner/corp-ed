"""Извлечение текста из загруженных файлов → Markdown (BH-2).

Выбор библиотек — рекомендация ML по замеру на 6 реальных документах:
- docx: mammoth → HTML → markdownify (заголовки → #, таблицы, списки);
  колонтитулы mammoth пропускает — их текст читается отдельно;
- pdf: pymupdf4llm (заголовки и таблицы; markitdown даёт ноль
  заголовков), страницы склеиваются PAGE_BREAK (\\f) — по ним preprocess
  находит колонтитулы;
- txt, md: как есть; UTF-8, а ещё UTF-16 с BOM и Windows-1251;
- xlsx, pptx, doc (Р-5, BH-33…BH-35): разбор ML — ingest/xlsx.py,
  ingest/pptx.py, ingest/doc.py, только стандартная библиотека. Каждый
  включён флагом INGEST_EXTRA_FORMATS: формат выключается без выкладки
  кода, если его качество на живых данных упадёт.

Файл пришёл от клиента и считается враждебным. Здесь — проверки,
которые дешёво сделать до парсера: формат по сигнатуре, а не по
расширению; zip-бомба в docx, xlsx и pptx; зашифрованный PDF и
документ Office с паролем; число страниц.
Сам разбор запускается в отдельном процессе с лимитами
(ingest/sandbox.py), этот модуль не вызывается из API напрямую.

Лицензии: pymupdf и pymupdf4llm — AGPL-3.0 (решение команды 25.09,
риск записан в RISKS.md). mammoth — BSD-2, markdownify — MIT.
"""

import io
import re
import zipfile
from collections.abc import Callable, Collection
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePath

from corp_ed.core.config import get_ingest_settings
from corp_ed.ingest.preprocess import PAGE_BREAK


class SourceFormat(StrEnum):
    DOCX = "docx"
    PDF = "pdf"
    TXT = "txt"
    MD = "md"
    XLSX = "xlsx"
    PPTX = "pptx"
    DOC = "doc"


EXTRA_FORMATS = (SourceFormat.XLSX, SourceFormat.PPTX, SourceFormat.DOC)
"""Р-5: форматы по одному, каждый — под флагом INGEST_EXTRA_FORMATS."""
_LISTED_ORDER = (
    SourceFormat.DOCX,
    SourceFormat.DOC,
    SourceFormat.XLSX,
    SourceFormat.PPTX,
    SourceFormat.PDF,
    SourceFormat.TXT,
    SourceFormat.MD,
)
"""Порядок в сообщении «Поддерживаются файлы …» (BH-35)."""


class ExtractionError(Exception):
    """Файл нельзя принять. code — для клиента и журнала, без деталей парсера."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# Сообщения для админа компании. Досье (3.3): неподдерживаемое
# отклоняется при загрузке с понятным сообщением.
ERROR_MESSAGES = {
    "unsupported_format": "Поддерживаются файлы docx, doc, xlsx, pptx, pdf, txt и md",
    "format_mismatch": "Содержимое файла не соответствует его расширению",
    "not_utf8": "Не удалось прочитать текстовый файл — сохраните его в кодировке UTF-8",
    "encrypted": "Файл защищён паролем — снимите защиту и загрузите снова",
    "no_text": "В файле нет текста (возможно, это скан без распознавания)",
    "too_many_pages": "Слишком много страниц в документе",
    "archive_too_large": "Файл распаковывается в слишком большой объём",
    "document_too_large": "Документ слишком большой",
    "corrupted": "Файл повреждён или не читается",
    "timeout": "Файл обрабатывается слишком долго",
}

# Частые форматы, которых ассистент не читает, — с советом, как быть
# (решение 28.09, П-3). Код ошибки прежний: unsupported_format. Совет
# зависит от того, какие из форматов Р-5 включены.
_WORD_HINT = "Этот формат Word не поддерживается — сохраните файл как .docx или PDF"


def _unsupported_hints(
    enabled: Collection[SourceFormat],
) -> dict[tuple[str, ...], str]:
    # .doc остаётся здесь и при включённом формате: так отвечает Word
    # 6.0/95, который ingest.doc не читает (BH-35, «уже существующая
    # подсказка»). Включённый .doc сюда с этим кодом иначе не попадёт.
    hints: dict[tuple[str, ...], str] = {(".doc", ".rtf", ".odt"): _WORD_HINT}
    if SourceFormat.PPTX in enabled:
        hints[(".ppt", ".odp", ".key")] = (
            "Этот формат презентаций не поддерживается — "
            "сохраните файл как .pptx или PDF"
        )
    else:
        hints[(".ppt", ".pptx", ".odp", ".key")] = (
            "Презентации пока не читаются — сохраните файл как PDF"
        )
    if SourceFormat.XLSX in enabled:
        hints[(".xls", ".ods", ".csv")] = (
            "Этот формат таблиц не поддерживается — сохраните файл как .xlsx или PDF"
        )
    else:
        hints[(".xls", ".xlsx", ".ods", ".csv")] = (
            "Таблицы пока не читаются — сохраните нужные листы как PDF"
        )
    return hints


def enabled_formats() -> frozenset[SourceFormat]:
    """docx, pdf, txt и md всегда; xlsx, pptx и doc — по флагу (Р-5)."""
    extra = {SourceFormat(name) for name in get_ingest_settings().extra_format_names}
    return (frozenset(SourceFormat) - frozenset(EXTRA_FORMATS)) | extra


def supported_message(enabled: Collection[SourceFormat] | None = None) -> str:
    formats = enabled_formats() if enabled is None else enabled
    names = [fmt.value for fmt in _LISTED_ORDER if fmt in formats]
    return f"Поддерживаются файлы {', '.join(names[:-1])} и {names[-1]}"


_TEXT_SUFFIXES = (".txt", ".md", ".markdown")


def error_message(code: str, filename: str | None = None) -> str:
    """Сообщение админу; для неподдерживаемого формата — с советом."""
    if code == "no_text" and filename and filename.lower().endswith(_TEXT_SUFFIXES):
        # Пустой .txt — не скан: совет про распознавание только путает.
        return "Файл пустой — в нём нет текста"
    if code != "unsupported_format":
        return ERROR_MESSAGES[code]
    enabled = enabled_formats()
    supported = supported_message(enabled)
    if filename:
        ext = PurePath(filename).suffix.lower()
        for extensions, hint in _unsupported_hints(enabled).items():
            if ext in extensions:
                return f"{hint}. {supported}"
    return supported


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
    ".xlsx": SourceFormat.XLSX,
    ".pptx": SourceFormat.PPTX,
    ".doc": SourceFormat.DOC,
}
_TEXT_IMPOSTORS = (b"%PDF-", b"PK\x03\x04", b"\xd0\xcf\x11\xe0", b"\x7fELF", b"MZ")
"""PDF, zip (docx, xlsx, pptx), OLE (doc, xls, ppt) и исполняемые файлы
с расширением .txt или .md."""


def supported_extensions() -> frozenset[str]:
    """Адаптерам коннекторов: что вообще стоит скачивать из источника.
    Выключенный формат Р-5 из источника тоже не скачивается."""
    enabled = enabled_formats()
    return frozenset(ext for ext, fmt in _EXTENSIONS.items() if fmt in enabled)


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
    if fmt is None or fmt not in enabled_formats():
        raise ExtractionError("unsupported_format")

    if fmt is SourceFormat.PDF and not data.startswith(b"%PDF-"):
        raise ExtractionError("format_mismatch")
    if fmt is SourceFormat.DOCX:
        _check_docx_container(data)
    if fmt in EXTRA_FORMATS:
        _office(fmt)[0](data)
    if fmt in (SourceFormat.TXT, SourceFormat.MD) and data.startswith(_TEXT_IMPOSTORS):
        raise ExtractionError("format_mismatch")
    return DetectedFile(format=fmt, filename=name)


_Check = Callable[[bytes], None]
_Convert = Callable[[bytes], str]


def _office(fmt: SourceFormat) -> tuple[_Check, _Convert]:
    """Проверка контейнера и разбор формата Р-5 (код ML) с ошибками
    ExtractionError: коды OfficeFileError — те же. Импорт внутри — модули
    нужны только этим форматам."""
    from corp_ed.ingest import doc, pptx, xlsx
    from corp_ed.ingest.ooxml import OfficeFileError

    parsers: dict[SourceFormat, tuple[_Check, _Convert]] = {
        SourceFormat.XLSX: (xlsx.check_container, xlsx.xlsx_to_markdown),
        SourceFormat.PPTX: (pptx.check_container, pptx.pptx_to_markdown),
        SourceFormat.DOC: (doc.check_container, doc.doc_to_markdown),
    }
    check_container, to_markdown = parsers[fmt]

    def check(data: bytes) -> None:
        try:
            check_container(data)
        except OfficeFileError as exc:
            raise ExtractionError(exc.code) from exc

    def convert(data: bytes) -> str:
        try:
            return to_markdown(data)
        except OfficeFileError as exc:
            raise ExtractionError(exc.code) from exc

    return check, convert


def _check_docx_container(data: bytes) -> None:
    if data.startswith(b"\xd0\xcf\x11\xe0") and (
        "EncryptedPackage".encode("utf-16-le") in data
    ):
        # Word с паролем — OLE-контейнер, а не zip. Без этой проверки
        # клиент читал «не соответствует расширению» (стенд 02.10);
        # xlsx и pptx проверяет так же ooxml.check_package.
        raise ExtractionError("encrypted")
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


def extract(fmt: SourceFormat, data: bytes, *, pdf_layout: bool = True) -> str:
    """Файл → Markdown до preprocess. Вызывать в песочнице (sandbox.py).

    pdf_layout — модель разметки PDF (INGEST_PDF_LAYOUT, RISKS №40).
    """
    if fmt is SourceFormat.DOCX:
        markdown = _extract_docx(data)
    elif fmt is SourceFormat.PDF:
        markdown = _extract_pdf(data, layout=pdf_layout)
    elif fmt in EXTRA_FORMATS:
        markdown = _office(fmt)[1](data)
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
    """UTF-8, а если не он — UTF-16 с BOM («Юникод» старого Блокнота)
    или Windows-1251: так сохраняют старые программы Windows, и такие
    .txt у клиентов есть (стенд 02.10: файл отклонялся)."""
    try:
        # utf-8-sig: BOM от Блокнота Windows не должен попасть в текст.
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
            return data.decode("utf-16")
        text = data.decode("cp1251")
    except UnicodeDecodeError as exc:
        raise ExtractionError("not_utf8") from exc
    if not _looks_russian(text):
        raise ExtractionError("not_utf8")
    return text


def _looks_russian(text: str) -> bool:
    """Windows-1251 декодирует почти любые байты; принимаем, только если
    вышел русский текст: больше половины букв — русские, управляющих
    символов почти нет. Иначе это другая кодировка или двоичный файл."""
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return False
    russian = sum("А" <= ch <= "я" or ch in "Ёё" for ch in letters)
    controls = sum(ch < " " and ch not in "\t\n\r\f" for ch in text)
    return russian > 0.5 * len(letters) and controls <= len(text) // 1000


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
    headers = _docx_running_text(data, "header")
    footers = _docx_running_text(data, "footer")
    # Верхний колонтитул — перед текстом: номер положения и редакция
    # попадают в первый фрагмент рядом с названием; нижний — в конец.
    parts = [*headers, markdown, *footers]
    return "\n\n".join(part for part in parts if part.strip())


_RUNNING_PART = re.compile(r"word/(header|footer)\d*\.xml")


def _docx_running_text(data: bytes, kind: str) -> list[str]:
    """Тексты колонтитулов docx без повторов.

    Там бывают номер положения, редакция, телефон ответственного, а
    mammoth колонтитулы не читает (стенд 02.10: «П-ОТП-07» из верхнего
    колонтитула не находился). Номера страниц — поля, их кэш из одних
    цифр отбрасывается.
    """
    from corp_ed.ingest import ooxml

    blocks: list[str] = []
    try:
        with ooxml.open_archive(data) as archive:
            for part in sorted(archive.namelist()):
                match = _RUNNING_PART.fullmatch(part)
                if match is None or match.group(1) != kind:
                    continue
                lines = []
                for _, element in ooxml.parse(archive, part):
                    if ooxml.local(element.tag) != "p":
                        continue
                    text = "".join(
                        node.text or ""
                        for node in element.iter()
                        if ooxml.local(node.tag) == "t"
                    ).strip()
                    if text and not text.isdigit():
                        lines.append(text)
                block = "\n\n".join(lines)
                if block and block not in blocks:
                    blocks.append(block)
    except Exception:  # noqa: BLE001 — любая ошибка разбора колонтитула
        # Колонтитулы — добавка: сломанные не должны валить документ,
        # который mammoth уже прочитал.
        return []
    return blocks


def _extract_pdf(data: bytes, *, layout: bool = True) -> str:
    import pymupdf
    import pymupdf4llm  # type: ignore[import-untyped]

    # Глобальный переключатель библиотеки: песочница — отдельный процесс
    # на каждый файл, так что состояние не утекает в другие разборы.
    pymupdf4llm.use_layout(layout)

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
