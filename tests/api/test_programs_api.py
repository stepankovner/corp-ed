from uuid import uuid4

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import Brief, Program, ProgramStatus, User


def _draft(brief: Brief, intern_id: None | User = None) -> Program:
    return Program(
        id=uuid4(),
        tenant_id=brief.tenant_id,
        brief_id=brief.id,
        intern_id=intern_id.id if intern_id else None,
        content="Неделя 1. Знакомство с командой.",
    )


async def test_manager_can_list_programs(
    manager_client: httpx.AsyncClient,
    brief: Brief,
    session: AsyncSession,
) -> None:
    program = _draft(brief)
    session.add(program)
    await session.commit()

    response = await manager_client.get("/api/v1/programs")

    assert response.status_code == 200

    body = response.json()

    assert len(body) == 1
    assert body[0]["id"] == str(program.id)
    assert body[0]["status"] == "draft"
    # Должность подтягивается из брифа: по одному id список не читается.
    assert body[0]["role_title"] == brief.role_title
    # Содержимое в списке не нужно: его отдаёт GET /programs/{id}.
    assert "content" not in body[0]


async def test_intern_cannot_list_programs(
    intern_client: httpx.AsyncClient,
) -> None:
    response = await intern_client.get("/api/v1/programs")

    assert response.status_code == 403


async def test_manager_can_edit_draft(
    manager_client: httpx.AsyncClient,
    brief: Brief,
    intern: User,
    session: AsyncSession,
) -> None:
    program = _draft(brief)
    session.add(program)
    await session.commit()

    response = await manager_client.patch(
        f"/api/v1/programs/{program.id}",
        json={"content": "Исправленный текст программы.", "intern_id": str(intern.id)},
    )

    assert response.status_code == 200

    body = response.json()

    assert body["content"] == "Исправленный текст программы."
    assert body["intern_id"] == str(intern.id)
    assert body["role_title"] == brief.role_title

    await session.refresh(program)

    assert program.content == "Исправленный текст программы."
    assert program.intern_id == intern.id


async def test_approve_without_intern_is_rejected(
    manager_client: httpx.AsyncClient,
    brief: Brief,
    session: AsyncSession,
) -> None:
    """Программа без стажёра после утверждения не видна никому."""
    program = _draft(brief)
    session.add(program)
    await session.commit()

    response = await manager_client.post(f"/api/v1/programs/{program.id}/approve")

    assert response.status_code == 409

    await session.refresh(program)

    assert program.status is ProgramStatus.DRAFT


async def test_manager_can_approve_assigned_program(
    manager_client: httpx.AsyncClient,
    brief: Brief,
    intern: User,
    session: AsyncSession,
) -> None:
    program = _draft(brief, intern)
    session.add(program)
    await session.commit()

    response = await manager_client.post(f"/api/v1/programs/{program.id}/approve")

    assert response.status_code == 200
    assert response.json()["status"] == "approved"

    await session.refresh(program)

    assert program.status is ProgramStatus.APPROVED


async def test_second_approve_does_not_fail(
    manager_client: httpx.AsyncClient,
    brief: Brief,
    intern: User,
    session: AsyncSession,
) -> None:
    """Два нажатия подряд не должны заканчиваться пятисоткой."""
    program = _draft(brief, intern)
    session.add(program)
    await session.commit()

    first = await manager_client.post(f"/api/v1/programs/{program.id}/approve")
    second = await manager_client.post(f"/api/v1/programs/{program.id}/approve")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "approved"


async def test_approved_program_cannot_be_edited(
    manager_client: httpx.AsyncClient,
    brief: Brief,
    intern: User,
    session: AsyncSession,
) -> None:
    program = _draft(brief, intern)
    program.status = ProgramStatus.APPROVED
    session.add(program)
    await session.commit()

    response = await manager_client.patch(
        f"/api/v1/programs/{program.id}",
        json={"content": "Подмена текста задним числом."},
    )

    assert response.status_code == 409


async def test_intern_cannot_approve(
    intern_client: httpx.AsyncClient,
    brief: Brief,
    session: AsyncSession,
) -> None:
    program = _draft(brief)
    session.add(program)
    await session.commit()

    response = await intern_client.post(f"/api/v1/programs/{program.id}/approve")

    assert response.status_code == 403


async def test_intern_does_not_see_draft(
    intern_client: httpx.AsyncClient,
    brief: Brief,
    intern: User,
    session: AsyncSession,
) -> None:
    """Черновик назначен стажёру, но не утверждён — он его не видит."""
    program = _draft(brief, intern)
    session.add(program)
    await session.commit()

    response = await intern_client.get("/api/v1/programs/my")

    assert response.status_code == 404


async def test_intern_sees_approved_program(
    intern_client: httpx.AsyncClient,
    brief: Brief,
    intern: User,
    session: AsyncSession,
) -> None:
    program = _draft(brief, intern)
    program.status = ProgramStatus.APPROVED
    session.add(program)
    await session.commit()

    response = await intern_client.get("/api/v1/programs/my")

    assert response.status_code == 200

    body = response.json()

    assert body["id"] == str(program.id)
    assert body["role_title"] == brief.role_title


async def test_manager_has_no_own_program(
    manager_client: httpx.AsyncClient,
) -> None:
    """Ручка стажёра руководителю не предназначена."""
    response = await manager_client.get("/api/v1/programs/my")

    assert response.status_code == 403
