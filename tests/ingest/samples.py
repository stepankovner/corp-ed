# ruff: noqa: E501 — URI пространств имён OOXML длиннее строки, а разрыв
# внутри XML-литерала читается хуже, чем длинная строка.
"""Файлы для тестов извлечения, собранные в коде — без бинарников в git."""

import io
import zipfile

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

_DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>
</w:styles>"""

_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _paragraph(text: str, style: str | None = None) -> str:
    props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{props}<w:r><w:t>{text}</w:t></w:r></w:p>"


def docx(
    paragraphs: list[tuple[str, str | None]],
    *,
    body_xml: str | None = None,
    extra_parts: dict[str, str] | None = None,
) -> bytes:
    body = body_xml or "".join(_paragraph(text, style) for text, style in paragraphs)
    document = (
        f'<?xml version="1.0" encoding="UTF-8"?><w:document {_W}>'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _RELS)
        archive.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        archive.writestr("word/styles.xml", _STYLES)
        archive.writestr("word/document.xml", document)
        for name, xml in (extra_parts or {}).items():
            archive.writestr(name, xml)
    return buffer.getvalue()


def running_part(tag: str, lines: list[str]) -> str:
    """Колонтитул docx: tag — hdr или ftr."""
    body = "".join(_paragraph(line, None) for line in lines)
    return f'<?xml version="1.0" encoding="UTF-8"?><w:{tag} {_W}>{body}</w:{tag}>'


def pdf(pages: list[list[tuple[str, int]]]) -> bytes:
    """PDF из строк (текст, кегль) — конструктором ML (pdf_samples.py), без
    pymupdf. Латиница: стандартный шрифт Helvetica без кириллицы.
    Зашифрованный PDF — готовый файл fixtures/encrypted.pdf."""
    from tests.ingest.pdf_samples import HEIGHT, Page
    from tests.ingest.pdf_samples import pdf as build

    built = []
    for lines in pages:
        page = Page()
        y = HEIGHT - 72
        for text, size in lines:
            page.text(72, y, text, size=size)
            y -= size * 2
        built.append(page)
    return build(built)


def zip_bomb_docx() -> bytes:
    """Валидный по структуре docx с членом, сжатым в сотни раз."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("word/document.xml", "<a/>" + " " * (50 * 1024 * 1024))
    return buffer.getvalue()
