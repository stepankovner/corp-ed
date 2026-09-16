from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import NotFoundError
from corp_ed.domain.models import (
    Chunk,
    Material,
    Tenant,
)
from corp_ed.llm.fake_embedding import FakeEmbeddingAdapter
from corp_ed.services.material_service import MaterialService


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

    assert chunks[0].content == "Первый абзац."
    assert chunks[1].content == "Второй абзац."
    assert chunks[2].content == "Третий абзац."


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

    reconstructed = "\n\n".join(chunk.content for chunk in chunks)

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
