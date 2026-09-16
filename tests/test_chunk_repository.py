from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import TenantContextMissingError
from corp_ed.core.tenant_context import current_tenant
from corp_ed.domain.models import Chunk, Material, Tenant, Track
from corp_ed.domain.types import ChunkMatch
from corp_ed.repositories.chunk_repository import ChunkRepository


async def test_search_returns_chunkmatch_with_distance(
    chunk_repo: ChunkRepository,
    material: Material,
) -> None:
    chunks = [
        Chunk(
            material_id=material.id,
            position=0,
            content="Первый чанк.",
            embedding=[0.1] * 256,
            model="fake",
            model_version="fake",
        ),
        Chunk(
            material_id=material.id,
            position=1,
            content="Второй чанк.",
            embedding=[0.2] * 256,
            model="fake",
            model_version="fake",
        ),
    ]

    await chunk_repo.bulk_create(chunks=chunks)

    result = await chunk_repo.search(embedding=[0.3] * 256)

    assert result
    assert all(isinstance(item, ChunkMatch) for item in result)
    assert all(isinstance(item.distance, float) for item in result)
    assert all(item.content for item in result)


async def test_search_respects_limit(
    chunk_repo: ChunkRepository,
    material: Material,
) -> None:
    chunks = [
        Chunk(
            material_id=material.id,
            position=position,
            content=f"Чанк {position}.",
            embedding=[0.1 + position / 10] * 256,
            model="fake",
            model_version="fake",
        )
        for position in range(5)
    ]

    await chunk_repo.bulk_create(chunks=chunks)

    result = await chunk_repo.search(
        embedding=[0.3] * 256,
        limit=2,
    )

    assert len(result) == 2


async def test_search_returns_nearest_chunk_first(
    chunk_repo: ChunkRepository,
    material: Material,
) -> None:
    chunks = [
        Chunk(
            material_id=material.id,
            position=0,
            content="Первый чанк.",
            embedding=[0.1] * 256,
            model="fake",
            model_version="fake",
        ),
        Chunk(
            material_id=material.id,
            position=1,
            content="Второй чанк.",
            embedding=[0.9] + [0.1] * 255,
            model="fake",
            model_version="fake",
        ),
    ]

    await chunk_repo.bulk_create(chunks=chunks)

    result = await chunk_repo.search(embedding=[0.1] * 256)

    assert result
    assert result[0].content == "Первый чанк."


async def test_search_does_not_return_foreign_tenant_chunk(
    chunk_repo: ChunkRepository,
    session: AsyncSession,
    material: Material,
    tenant_ctx: Tenant,
) -> None:
    foreign_tenant = Tenant(
        id=uuid4(),
        company_code="other",
        name="Other Co",
    )
    session.add(foreign_tenant)
    await session.commit()

    current_tenant.set(foreign_tenant.id)

    foreign_material = Material(
        id=uuid4(),
        tenant_id=foreign_tenant.id,
        track=Track.MARKETING,
        title="Foreign tenant material",
        content="Foreign tenant material",
    )
    session.add(foreign_material)
    await session.commit()

    foreign_chunks = [
        Chunk(
            material_id=foreign_material.id,
            position=0,
            content="Первый чанк чужого тенанта.",
            embedding=[0.1] * 256,
            model="fake",
            model_version="fake",
        ),
        Chunk(
            material_id=foreign_material.id,
            position=1,
            content="Второй чанк чужого тенанта.",
            embedding=[0.2] * 256,
            model="fake",
            model_version="fake",
        ),
    ]

    await chunk_repo.bulk_create(chunks=foreign_chunks)

    current_tenant.set(tenant_ctx.id)

    result = await chunk_repo.search(embedding=[0.1] * 256)

    assert result == []


async def test_search_requires_tenant_context(
    chunk_repo: ChunkRepository,
) -> None:
    token = current_tenant.set(None)
    try:
        with pytest.raises(TenantContextMissingError):
            await chunk_repo.search(embedding=[0.1] * 256)
    finally:
        current_tenant.reset(token)
