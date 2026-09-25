from uuid import UUID, uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import Chunk, Material, User
from corp_ed.llm.fake_embedding import FakeEmbeddingAdapter


async def test_admin_can_create_material(
    admin_client: httpx.AsyncClient,
    admin: User,
    session: AsyncSession,
) -> None:
    response = await admin_client.post(
        "/api/v1/materials",
        json={
            "title": "Регламент отпусков",
            "content": "Первый абзац.",
        },
    )

    assert response.status_code == 201

    body = response.json()

    assert "id" in body
    assert "content" not in body

    material = await session.get(Material, UUID(body["id"]))

    assert material is not None
    assert material.tenant_id == admin.tenant_id


async def test_employee_cannot_create_material(
    employee_client: httpx.AsyncClient,
    session: AsyncSession,
) -> None:
    response = await employee_client.post(
        "/api/v1/materials",
        json={
            "title": "Регламент отпусков",
            "content": "Первый абзац.",
        },
    )

    assert response.status_code == 403

    result = await session.execute(select(Material))
    materials = result.scalars().all()

    assert materials == []


async def test_create_material_requires_authentication(
    api: httpx.AsyncClient,
) -> None:
    response = await api.post(
        "/api/v1/materials",
        json={
            "title": "Регламент отпусков",
            "content": "Первый абзац.",
        },
    )

    assert response.status_code == 401


async def test_ingest_material(
    admin_client: httpx.AsyncClient,
    material: Material,
    session: AsyncSession,
    fake_embeddings: FakeEmbeddingAdapter,
) -> None:
    response = await admin_client.post(
        f"/api/v1/materials/{material.id}/ingest",
    )

    assert response.status_code == 200

    body = response.json()

    assert body["chunks"] == 3

    result = await session.execute(
        select(Chunk).where(Chunk.material_id == material.id)
    )
    chunks = result.scalars().all()

    assert len(chunks) == 3

    # FakeEmbeddingAdapter из общей fixture.
    assert len(fake_embeddings.document_calls) == 3


async def test_ingest_material_not_found(
    admin_client: httpx.AsyncClient,
) -> None:
    response = await admin_client.post(
        f"/api/v1/materials/{uuid4()}/ingest",
    )

    assert response.status_code == 404
