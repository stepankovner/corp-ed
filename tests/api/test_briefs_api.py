from uuid import UUID, uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import Brief, User

PAYLOAD = {
    "track": "marketing",
    "role_title": "Стажёр-маркетолог",
    "goals": "Вести контент-план одного продукта самостоятельно.",
    "tasks": "Тексты для блога, сбор статистики, ежемесячный отчёт.",
    "intern_level": "Junior, четвёртый курс",
}


async def test_manager_can_create_brief(
    manager_client: httpx.AsyncClient,
    manager: User,
    session: AsyncSession,
) -> None:
    response = await manager_client.post("/api/v1/briefs", json=PAYLOAD)

    assert response.status_code == 201

    body = response.json()

    assert body["role_title"] == PAYLOAD["role_title"]
    assert body["track"] == "marketing"

    brief = await session.get(Brief, UUID(body["id"]))

    assert brief is not None
    assert brief.author_id == manager.id
    assert brief.tenant_id == manager.tenant_id


async def test_intern_cannot_create_brief(
    intern_client: httpx.AsyncClient,
    session: AsyncSession,
) -> None:
    response = await intern_client.post("/api/v1/briefs", json=PAYLOAD)

    assert response.status_code == 403

    result = await session.execute(select(Brief))

    assert result.scalars().all() == []


async def test_create_brief_ignores_author_id_from_body(
    manager_client: httpx.AsyncClient,
    manager: User,
    session: AsyncSession,
) -> None:
    """Автор берётся из токена: подставить чужой id через тело нельзя."""
    response = await manager_client.post(
        "/api/v1/briefs",
        json={**PAYLOAD, "author_id": str(uuid4())},
    )

    assert response.status_code == 201

    brief = await session.get(Brief, UUID(response.json()["id"]))

    assert brief is not None
    assert brief.author_id == manager.id


async def test_create_brief_rejects_empty_field(
    manager_client: httpx.AsyncClient,
) -> None:
    response = await manager_client.post(
        "/api/v1/briefs",
        json={**PAYLOAD, "role_title": ""},
    )

    assert response.status_code == 422


async def test_create_brief_requires_authentication(
    api: httpx.AsyncClient,
) -> None:
    response = await api.post("/api/v1/briefs", json=PAYLOAD)

    assert response.status_code == 401


async def test_manager_can_list_briefs(
    manager_client: httpx.AsyncClient,
    brief: Brief,
) -> None:
    response = await manager_client.get("/api/v1/briefs")

    assert response.status_code == 200

    body = response.json()

    assert len(body) == 1
    assert body[0]["id"] == str(brief.id)
    assert body[0]["role_title"] == brief.role_title


async def test_intern_cannot_list_briefs(
    intern_client: httpx.AsyncClient,
) -> None:
    response = await intern_client.get("/api/v1/briefs")

    assert response.status_code == 403
