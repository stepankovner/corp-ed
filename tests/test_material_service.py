from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import NotFoundError
from corp_ed.domain.models import (
    Chunk,
    Material,
    Tenant,
    Track,
)
from corp_ed.llm.fake_embedding import FakeEmbeddingAdapter
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.repositories.material_repository import MaterialRepository
from corp_ed.services.material_service import MaterialService


def _roomy_service(
    session: AsyncSession, embeddings: FakeEmbeddingAdapter
) -> MaterialService:
    """Сервис с боевым размером чанка: короткий документ — один чанк."""
    return MaterialService(
        material_repo=MaterialRepository(session),
        chunk_repo=ChunkRepository(session),
        embedding_gateway=embeddings,
        session=session,
        chunk_tokens=400,
        overlap_tokens=50,
    )


async def test_ingest_create_chunks(
    material: Material,
    material_service: MaterialService,
    session: AsyncSession,
) -> None:
    length: int = await material_service.ingest(material.id)

    stmt = select(Chunk).where(Chunk.material_id == material.id)

    result = await session.execute(stmt)
    chunks = result.scalars().all()

    assert length > 0
    assert length == len(chunks)


async def test_positions_are_sorted(
    material: Material,
    material_service: MaterialService,
    session: AsyncSession,
) -> None:
    length: int = await material_service.ingest(material.id)

    stmt = (
        select(Chunk).where(Chunk.material_id == material.id).order_by(Chunk.position)
    )

    result = await session.execute(stmt)
    chunks = result.scalars().all()

    assert len(chunks) == length

    positions = [chunk.position for chunk in chunks]
    assert positions == list(range(length))

    # Первая строка чанка — крошки: название документа (BH-3).
    assert chunks[0].content == "Регламент отпусков\nПервый абзац."
    assert chunks[1].content == "Регламент отпусков\nВторой абзац."
    assert chunks[2].content == "Регламент отпусков\nТретий абзац."


async def test_chunks_content_matches_material(
    material: Material,
    material_service: MaterialService,
    session: AsyncSession,
) -> None:
    await material_service.ingest(material.id)

    stmt = (
        select(Chunk).where(Chunk.material_id == material.id).order_by(Chunk.position)
    )

    result = await session.execute(stmt)
    chunks = result.scalars().all()

    # Без крошек тела чанков собираются обратно в исходный текст:
    # нарезка ничего не теряет и не дублирует при overlap=0.
    bodies = [chunk.content.split("\n", 1)[1] for chunk in chunks]
    reconstructed = "\n\n".join(bodies)

    assert reconstructed == material.content


async def test_ingest_call_embed_document(
    material: Material,
    material_service: MaterialService,
    fake_embeddings: FakeEmbeddingAdapter,
) -> None:
    length: int = await material_service.ingest(material.id)

    assert len(fake_embeddings.document_calls) == length
    assert fake_embeddings.query_calls == []


async def test_metadata_into_database(
    material: Material,
    material_service: MaterialService,
    session: AsyncSession,
) -> None:
    await material_service.ingest(material.id)

    stmt = select(Chunk).where(Chunk.material_id == material.id)

    result = await session.execute(stmt)
    chunks = result.scalars().all()

    assert chunks
    assert all(
        chunk.model == "text-search-doc" and chunk.model_version == "fake"
        for chunk in chunks
    )


async def test_subsequent_ingest_replaces_chunks(
    material: Material,
    material_service: MaterialService,
    session: AsyncSession,
) -> None:
    first_length = await material_service.ingest(material.id)
    second_length = await material_service.ingest(material.id)

    stmt = select(Chunk).where(Chunk.material_id == material.id)
    result = await session.execute(stmt)
    chunks = result.scalars().all()

    assert second_length == first_length
    assert len(chunks) == first_length


async def test_material_not_found(
    material_service: MaterialService,
    tenant_ctx: Tenant,
) -> None:

    with pytest.raises(NotFoundError):
        await material_service.ingest(uuid4())


async def test_empty_content_clears_old_chunks(
    material: Material,
    material_service: MaterialService,
    session: AsyncSession,
) -> None:
    await material_service.ingest(material.id)

    material.content = ""
    await session.commit()

    length: int = await material_service.ingest(material.id)

    stmt = select(Chunk).where(Chunk.material_id == material.id)

    result = await session.execute(stmt)
    chunks = result.scalars().all()

    assert length == 0
    assert chunks == []


async def test_embedding_is_computed_from_embed_text(
    session: AsyncSession,
    tenant_ctx: Tenant,
    fake_embeddings: FakeEmbeddingAdapter,
) -> None:
    """В эмбеддинг уходит текст без разметки, в промпт — Markdown (BH-3)."""
    material = Material(
        track=Track.MARKETING,
        title="Положение.docx",
        content="# Раздел 1\n\nСрок — **14 дней**.",
    )
    session.add(material)
    await session.commit()

    await _roomy_service(session, fake_embeddings).ingest(material.id)

    result = await session.execute(
        select(Chunk).where(Chunk.material_id == material.id)
    )
    chunk = result.scalars().one()

    assert fake_embeddings.document_calls == [chunk.embed_text]
    assert "**" not in chunk.embed_text
    assert "**14 дней**" in chunk.content


async def test_heading_path_is_stored(
    session: AsyncSession,
    tenant_ctx: Tenant,
    fake_embeddings: FakeEmbeddingAdapter,
) -> None:
    material = Material(
        track=Track.MARKETING,
        title="Положение об отпусках",
        content="# Раздел 3\n\n## 3.2 Перенос отпуска\n\nПо заявлению.",
    )
    session.add(material)
    await session.commit()

    await _roomy_service(session, fake_embeddings).ingest(material.id)

    result = await session.execute(
        select(Chunk).where(Chunk.material_id == material.id)
    )
    chunk = result.scalars().one()

    assert chunk.heading_path == ["Раздел 3", "3.2 Перенос отпуска"]
    # Расширение файла в крошки не попадает, название документа — первым.
    assert chunk.content.startswith(
        "Положение об отпусках > Раздел 3 > 3.2 Перенос отпуска\n"
    )
