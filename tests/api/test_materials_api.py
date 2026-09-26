from uuid import UUID, uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import (
    IngestJob,
    IngestJobStatus,
    Material,
    Tenant,
    User,
)
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


async def test_created_material_is_queued_for_ingest(
    admin_client: httpx.AsyncClient,
    session: AsyncSession,
) -> None:
    """Материал и задача — одной транзакцией (очередь в Postgres)."""
    response = await admin_client.post(
        "/api/v1/materials",
        json={"title": "Регламент отпусков", "content": "Первый абзац."},
    )

    assert response.status_code == 201
    assert response.json()["status"] == "pending"

    jobs = (await session.execute(select(IngestJob))).scalars().all()
    assert [job.material_id for job in jobs] == [UUID(response.json()["id"])]
    assert jobs[0].status is IngestJobStatus.QUEUED


async def test_ingest_request_returns_202_and_queues_once(
    admin_client: httpx.AsyncClient,
    material: Material,
    session: AsyncSession,
    fake_embeddings: FakeEmbeddingAdapter,
) -> None:
    """Сама индексация — в воркере; ручка отвечает сразу и идемпотентна."""
    first = await admin_client.post(f"/api/v1/materials/{material.id}/ingest")
    second = await admin_client.post(f"/api/v1/materials/{material.id}/ingest")

    assert first.status_code == 202
    assert first.json() == {"material_id": str(material.id), "status": "pending"}
    assert second.status_code == 202

    jobs = (await session.execute(select(IngestJob))).scalars().all()
    assert len(jobs) == 1
    # Эмбеддинги в запросе не считаются.
    assert fake_embeddings.document_calls == []


async def test_ingest_material_not_found(
    admin_client: httpx.AsyncClient,
) -> None:
    response = await admin_client.post(
        f"/api/v1/materials/{uuid4()}/ingest",
    )

    assert response.status_code == 404


async def test_admin_lists_and_reads_materials(
    admin_client: httpx.AsyncClient,
    material: Material,
) -> None:
    listed = await admin_client.get("/api/v1/materials")
    one = await admin_client.get(f"/api/v1/materials/{material.id}")

    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [str(material.id)]
    assert one.json()["title"] == material.title
    # Текст документа наружу не отдаётся — только метаданные и статус.
    assert "content" not in one.json()


async def test_employee_cannot_list_materials(
    employee_client: httpx.AsyncClient,
) -> None:
    assert (await employee_client.get("/api/v1/materials")).status_code == 403


async def test_admin_cannot_read_other_company_material(
    admin_client: httpx.AsyncClient,
    session: AsyncSession,
) -> None:
    other = Tenant(id=uuid4(), company_code="other", name="Other")
    session.add(other)
    await session.commit()
    with tenant_scope(other.id):
        foreign = Material(tenant_id=other.id, title="Чужой", content="x")
        session.add(foreign)
        await session.commit()

    read = await admin_client.get(f"/api/v1/materials/{foreign.id}")
    ingest = await admin_client.post(f"/api/v1/materials/{foreign.id}/ingest")

    assert read.status_code == 404
    assert ingest.status_code == 404
