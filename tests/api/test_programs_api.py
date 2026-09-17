from uuid import uuid4

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import Brief, Program


async def test_manager_can_list_programs(
    manager_client: httpx.AsyncClient,
    brief: Brief,
    session: AsyncSession,
) -> None:
    program = Program(
        id=uuid4(),
        tenant_id=brief.tenant_id,
        brief_id=brief.id,
        content="Неделя 1. Знакомство с командой.",
    )
    session.add(program)
    await session.commit()

    response = await manager_client.get("/api/v1/programs")

    assert response.status_code == 200

    body = response.json()

    assert len(body) == 1
    assert body[0]["id"] == str(program.id)
    assert body[0]["brief_id"] == str(brief.id)
    assert body[0]["status"] == "draft"
    # Содержимое в списке не нужно: его отдаёт GET /programs/{id}.
    assert "content" not in body[0]


async def test_intern_cannot_list_programs(
    intern_client: httpx.AsyncClient,
) -> None:
    response = await intern_client.get("/api/v1/programs")

    assert response.status_code == 403
