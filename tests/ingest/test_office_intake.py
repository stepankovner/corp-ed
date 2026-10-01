"""Приём .xlsx, .pptx и .doc (Р-5, BH-33…BH-35): флаг, подмены, путь до чанков.

Разбор форматов — код ML (ingest/xlsx.py, pptx.py, doc.py) и его тесты.
Здесь — то, что встраивает бэкенд: detect_format и сообщения под флагом
INGEST_EXTRA_FORMATS, разбор в песочнице и чанки после preprocess.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.config import IngestSettings
from corp_ed.domain.models import Chunk, Material, Tenant
from corp_ed.ingest import extract as extract_module
from corp_ed.ingest.extract import (
    ExtractionError,
    SourceFormat,
    detect_format,
    enabled_formats,
    error_message,
    supported_extensions,
)
from corp_ed.ingest.sandbox import extract_isolated
from corp_ed.llm.fake_embedding import FakeEmbeddingAdapter
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.repositories.material_repository import MaterialRepository
from corp_ed.services.ingest_service import IngestService
from tests.ingest import samples
from tests.ingest.doc_samples import Para, Style, cfb, doc
from tests.ingest.pptx_samples import SlideSpec, paragraph, pptx, text_shape, title
from tests.ingest.xlsx_samples import SheetSpec, xlsx

WORKBOOK = xlsx(
    [
        SheetSpec(
            "Суточные",
            {
                "A1": "Направление",
                "B1": "Суточные в день",
                "A2": "Остальные страны",
                "B2": "2 500 рублей",
            },
        )
    ]
)
DECK = pptx(
    [
        SlideSpec(
            [
                title("Первый день"),
                text_shape([paragraph("Пропуск выдают на ресепшене")]),
            ]
        )
    ]
)
WORD_97 = doc(
    [
        Para("Положение о командировках", istd=1),
        Para("Суточные по России — 700 рублей."),
    ],
    styles=[Style(0, 0, "Normal"), Style(1, 1, "heading 1")],
)
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _code(call: object) -> str:
    with pytest.raises(ExtractionError) as info:
        call()  # type: ignore[operator]
    return info.value.code


def _flag(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    settings = IngestSettings(extra_formats=value)
    monkeypatch.setattr(extract_module, "get_ingest_settings", lambda: settings)


# --- флаг ------------------------------------------------------------------------


def test_all_three_formats_are_on_by_default() -> None:
    assert IngestSettings().extra_format_names == ["xlsx", "pptx", "doc"]
    assert {SourceFormat.XLSX, SourceFormat.PPTX, SourceFormat.DOC} <= enabled_formats()
    assert {".xlsx", ".pptx", ".doc"} <= supported_extensions()


def test_unknown_format_name_fails_at_start() -> None:
    with pytest.raises(ValueError, match="xls"):
        IngestSettings(extra_formats="xlsx,xls")


@pytest.mark.parametrize(
    ("filename", "data", "fmt"),
    [
        ("Суточные.XLSX", WORKBOOK, SourceFormat.XLSX),
        ("Онбординг.pptx", DECK, SourceFormat.PPTX),
        ("Положение.doc", WORD_97, SourceFormat.DOC),
    ],
)
def test_detects_office_formats(filename: str, data: bytes, fmt: SourceFormat) -> None:
    assert detect_format(filename, data).format is fmt


def test_format_off_is_rejected_with_advice(monkeypatch: pytest.MonkeyPatch) -> None:
    _flag(monkeypatch, "")

    for filename, data in (("a.xlsx", WORKBOOK), ("a.pptx", DECK), ("a.doc", WORD_97)):
        with pytest.raises(ExtractionError) as info:
            detect_format(filename, data)
        assert info.value.code == "unsupported_format"
    assert supported_extensions() == {".docx", ".pdf", ".txt", ".md", ".markdown"}
    assert error_message("unsupported_format", "a.xlsx") == (
        "Таблицы пока не читаются — сохраните нужные листы как PDF. "
        "Поддерживаются файлы docx, pdf, txt и md"
    )
    assert error_message("unsupported_format", "a.pptx").startswith(
        "Презентации пока не читаются — сохраните файл как PDF"
    )
    assert error_message("unsupported_format", "a.doc").startswith(
        "Этот формат Word не поддерживается — сохраните файл как .docx или PDF"
    )


def test_one_format_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _flag(monkeypatch, "xlsx,doc")

    assert detect_format("a.xlsx", WORKBOOK).format is SourceFormat.XLSX
    assert _code(lambda: detect_format("a.pptx", DECK)) == "unsupported_format"
    assert error_message("unsupported_format", "run.exe") == (
        "Поддерживаются файлы docx, doc, xlsx, pdf, txt и md"
    )


@pytest.mark.parametrize(
    ("filename", "advice"),
    [
        ("old.rtf", "сохраните файл как .docx или PDF"),
        ("slides.ppt", "сохраните файл как .pptx или PDF"),
        ("budget.xls", "сохраните файл как .xlsx или PDF"),
        ("data.csv", "сохраните файл как .xlsx или PDF"),
    ],
)
def test_advice_points_to_the_supported_office_format(
    filename: str, advice: str
) -> None:
    message = error_message("unsupported_format", filename)
    assert advice in message
    assert message.endswith("Поддерживаются файлы docx, doc, xlsx, pptx, pdf, txt и md")


# --- подмены и защищённые файлы ---------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "data", "code"),
    [
        ("a.xlsx", b"%PDF-1.7\n", "format_mismatch"),
        ("a.xlsx", samples.docx([("Текст", None)]), "format_mismatch"),
        ("a.pptx", WORKBOOK, "format_mismatch"),
        ("a.xlsx", DECK, "format_mismatch"),
        ("a.doc", cfb({"Workbook": b"\0" * 5000}), "format_mismatch"),
        ("a.doc", "{\\rtf1\\ansi текст}".encode("cp1251"), "format_mismatch"),
        ("a.xlsx", OLE + "EncryptedPackage".encode("utf-16-le"), "encrypted"),
        (
            "a.doc",
            cfb({"EncryptedPackage": b"\0" * 100, "EncryptionInfo": b"\0" * 10}),
            "encrypted",
        ),
    ],
    ids=[
        "pdf-as-xlsx",
        "docx-as-xlsx",
        "xlsx-as-pptx",
        "pptx-as-xlsx",
        "xls-as-doc",
        "rtf-as-doc",
        "xlsx-with-password",
        "doc-with-password",
    ],
)
def test_disguised_and_protected_files(filename: str, data: bytes, code: str) -> None:
    assert _code(lambda: detect_format(filename, data)) == code


def test_word_95_gets_the_word_advice() -> None:
    old = doc([Para("x")], flags=0x1200, nfib=0x0065)

    assert _code(lambda: detect_format("old.doc", old)) == "unsupported_format"
    assert error_message("unsupported_format", "old.doc").startswith(
        "Этот формат Word не поддерживается — сохраните файл как .docx или PDF"
    )


def test_ole_file_renamed_to_text_is_rejected() -> None:
    assert _code(lambda: detect_format("a.txt", WORD_97)) == "format_mismatch"


# --- песочница и чанки ------------------------------------------------------------


async def _chunks(
    session: AsyncSession, embeddings: FakeEmbeddingAdapter, markdown: str, fmt: str
) -> list[Chunk]:
    material = Material(title="Документ", content=markdown, source_format=fmt)
    session.add(material)
    await session.commit()
    await IngestService(
        material_repo=MaterialRepository(session),
        chunk_repo=ChunkRepository(session),
        embedding_gateway=embeddings,
        session=session,
        chunk_tokens=400,
        overlap_tokens=50,
    ).ingest(material.id)
    result = await session.execute(
        select(Chunk).where(Chunk.material_id == material.id).order_by(Chunk.position)
    )
    return list(result.scalars())


async def test_workbook_becomes_key_value_chunks(
    session: AsyncSession, tenant_ctx: Tenant, fake_embeddings: FakeEmbeddingAdapter
) -> None:
    markdown = await extract_isolated(SourceFormat.XLSX, WORKBOOK)

    chunks = await _chunks(session, fake_embeddings, markdown, "xlsx")

    text = "\n".join(chunk.content for chunk in chunks)
    assert "Направление: Остальные страны" in text
    assert "Суточные в день: 2 500 рублей" in text


async def test_slide_titles_become_headings(
    session: AsyncSession, tenant_ctx: Tenant, fake_embeddings: FakeEmbeddingAdapter
) -> None:
    markdown = await extract_isolated(SourceFormat.PPTX, DECK)

    chunks = await _chunks(session, fake_embeddings, markdown, "pptx")

    assert any("Первый день" in chunk.heading_path for chunk in chunks)
    assert any("Пропуск выдают на ресепшене" in chunk.content for chunk in chunks)


async def test_word_97_headings_survive(
    session: AsyncSession, tenant_ctx: Tenant, fake_embeddings: FakeEmbeddingAdapter
) -> None:
    markdown = await extract_isolated(SourceFormat.DOC, WORD_97)

    chunks = await _chunks(session, fake_embeddings, markdown, "doc")

    assert any("Положение о командировках" in chunk.heading_path for chunk in chunks)
    assert any("700 рублей" in chunk.content for chunk in chunks)


async def test_sandbox_passes_office_error_codes() -> None:
    with pytest.raises(ExtractionError) as info:
        await extract_isolated(SourceFormat.XLSX, b"PK\x03\x04 broken")
    assert info.value.code == "corrupted"
