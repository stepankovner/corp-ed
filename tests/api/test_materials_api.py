from uuid import UUID, uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.tenant_context import current_tenant
from corp_ed.domain.models import Chunk, Material, Tenant, Track, User
from corp_ed.llm.fake_embedding import FakeEmbeddingAdapter


async def test_manager_can_create_material(
    manager_client: httpx.AsyncClient,
    manager: User,
    session: AsyncSession,
) -> None:
    response = await manager_client.post(
        "/api/v1/materials",
        json={
            "track": "marketing",
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
    assert material.tenant_id == manager.tenant_id


async def test_intern_cannot_create_material(
    intern_client: httpx.AsyncClient,
    session: AsyncSession,
) -> None:
    response = await intern_client.post(
        "/api/v1/materials",
        json={
            "track": "marketing",
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
            "track": "marketing",
            "title": "Регламент отпусков",
            "content": "Первый абзац.",
        },
    )

    assert response.status_code == 401


async def test_ingest_material(
    manager_client: httpx.AsyncClient,
    material: Material,
    session: AsyncSession,
    fake_embeddings: FakeEmbeddingAdapter,
) -> None:
    response = await manager_client.post(
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
    manager_client: httpx.AsyncClient,
) -> None:
    response = await manager_client.post(
        f"/api/v1/materials/{uuid4()}/ingest",
    )

    assert response.status_code == 404


async def test_material_list_shows_indexing_state(
    manager_client: httpx.AsyncClient,
    material: Material,
) -> None:
    """Число чанков — это и есть отметка «проиндексирован» в интерфейсе."""
    response = await manager_client.get("/api/v1/materials")

    assert response.status_code == 200

    body = response.json()

    assert len(body) == 1
    assert body[0]["title"] == material.title
    assert body[0]["chunks"] == 0
    assert "content" not in body[0]

    await manager_client.post(f"/api/v1/materials/{material.id}/ingest")

    response = await manager_client.get("/api/v1/materials")

    assert response.json()[0]["chunks"] == 3


async def test_intern_cannot_list_materials(
    intern_client: httpx.AsyncClient,
) -> None:
    response = await intern_client.get("/api/v1/materials")

    assert response.status_code == 403


async def test_material_list_excludes_other_tenants(
    manager_client: httpx.AsyncClient,
    material: Material,
    session: AsyncSession,
) -> None:
    foreign_tenant = Tenant(id=uuid4(), company_code="other", name="Other Co")
    session.add(foreign_tenant)
    await session.commit()

    # Контекст переставляется на чужого тенанта: иначе хук записи
    # отклонит материал с чужим tenant_id.
    token = current_tenant.set(foreign_tenant.id)
    try:
        session.add(
            Material(
                id=uuid4(),
                tenant_id=foreign_tenant.id,
                track=Track.ANALYTICS,
                title="Материал чужой компании",
                content="Текст чужой компании.",
            )
        )
        await session.commit()
    finally:
        current_tenant.reset(token)

    response = await manager_client.get("/api/v1/materials")

    titles = [item["title"] for item in response.json()]

    assert titles == [material.title]
