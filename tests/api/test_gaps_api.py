"""GET/PATCH /api/v1/gaps (BH-23): роли, изоляция, маскирование, аудит."""

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import AuditEvent, GapCluster, Tenant, User
from tests.api.conftest import bearer
from tests.test_gap_report import ask, colleague, service, topic

URL = "/api/v1/gaps"


async def _build(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    user: User,
) -> None:
    """Два пробела: командировки (3 вопроса, 2 человека) и VPN (2)."""
    tenant = await session.get(Tenant, user.tenant_id)
    assert tenant is not None
    other = await colleague(session, tenant, "b@test.com")
    ask(session, user, "Как оформить командировку? Пишите ivan@corp.ru", topic(0))
    ask(session, other, "Суточные в командировке?", topic(0))
    ask(session, user, "Командировка за границу?", topic(0))
    ask(session, user, "Как подключить VPN?", topic(1))
    ask(session, user, "VPN не работает", topic(1))
    await session.commit()
    await service(session_maker).run(tenant.company_code)


async def test_admin_sees_gaps_by_priority(
    api: httpx.AsyncClient,
    admin_account: User,
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _build(session, session_maker, admin_account)

    response = await api.get(URL, headers=bearer(admin_account))

    assert response.status_code == 200
    first, second = response.json()["clusters"]
    assert first["priority"] > second["priority"]
    assert (first["question_count"], first["user_count"]) == (3, 2)
    assert first["status"] == "new"
    assert first["title"] == "Командировки"
    assert len(first["sample_questions"]) == 3
    assert set(first) == {
        "id",
        "title",
        "missing",
        "priority",
        "question_count",
        "user_count",
        "first_seen",
        "last_seen",
        "status",
        "sample_questions",
    }


async def test_sample_questions_are_masked(
    api: httpx.AsyncClient,
    admin_account: User,
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """В тестовом журнале вопрос записан как есть (в проде — уже после
    mask_pii): отчёт маскирует ещё раз на выходе."""
    await _build(session, session_maker, admin_account)

    response = await api.get(URL, headers=bearer(admin_account))

    assert "ivan@corp.ru" not in response.text


async def test_status_filter_and_limit(
    api: httpx.AsyncClient,
    admin_account: User,
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _build(session, session_maker, admin_account)
    [top] = (
        await api.get(URL, params={"limit": 1}, headers=bearer(admin_account))
    ).json()["clusters"]
    await api.patch(
        f"{URL}/{top['id']}",
        json={"status": "in_progress"},
        headers=bearer(admin_account),
    )

    in_progress = await api.get(
        URL, params={"status": "in_progress"}, headers=bearer(admin_account)
    )
    new = await api.get(URL, params={"status": "new"}, headers=bearer(admin_account))

    assert [c["id"] for c in in_progress.json()["clusters"]] == [top["id"]]
    assert len(new.json()["clusters"]) == 1


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"status": "x"}])
async def test_query_is_validated(
    api: httpx.AsyncClient, admin_account: User, params: dict[str, object]
) -> None:
    response = await api.get(URL, params=params, headers=bearer(admin_account))
    assert response.status_code == 422


async def test_employee_and_anonymous_cannot_see_gaps(
    api: httpx.AsyncClient, account: User
) -> None:
    assert (await api.get(URL, headers=bearer(account))).status_code == 403
    assert (await api.get(URL)).status_code == 401
    response = await api.patch(
        f"{URL}/{uuid4()}", json={"status": "resolved"}, headers=bearer(account)
    )
    assert response.status_code == 403


async def test_status_change_is_audited(
    api: httpx.AsyncClient,
    admin_account: User,
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    await _build(session, session_maker, admin_account)
    [top, _] = (await api.get(URL, headers=bearer(admin_account))).json()["clusters"]

    response = await api.patch(
        f"{URL}/{top['id']}",
        json={"status": "resolved"},
        headers=bearer(admin_account),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "resolved"
    assert len(response.json()["sample_questions"]) == 3
    event = (
        await session.execute(
            select(AuditEvent).where(AuditEvent.action == "gap.status_changed")
        )
    ).scalar_one()
    assert event.details == {"from": "new", "to": "resolved"}
    assert event.actor_user_id == admin_account.id


@pytest.mark.parametrize(
    "body",
    [{"status": "deleted"}, {}, {"status": "resolved", "priority": 1000}],
)
async def test_status_body_is_validated(
    api: httpx.AsyncClient, admin_account: User, body: dict[str, object]
) -> None:
    response = await api.patch(
        f"{URL}/{uuid4()}", json=body, headers=bearer(admin_account)
    )
    assert response.status_code == 422


async def test_other_company_gaps_are_invisible(
    api: httpx.AsyncClient,
    admin_account: User,
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    other = Tenant(id=uuid4(), company_code="other", name="Other")
    session.add(other)
    await session.commit()
    with tenant_scope(other.id):
        stranger = await colleague(session, other, "s@other.ru")
        await _build(session, session_maker, stranger)
        foreign = (await session.execute(select(GapCluster))).scalars().first()
        await session.commit()
    assert foreign is not None

    listed = await api.get(URL, headers=bearer(admin_account))
    patched = await api.patch(
        f"{URL}/{foreign.id}",
        json={"status": "dismissed"},
        headers=bearer(admin_account),
    )

    assert listed.json()["clusters"] == []
    assert patched.status_code == 404
    with tenant_scope(other.id):
        status = await session.scalar(
            select(GapCluster.status).where(GapCluster.id == foreign.id)
        )
        await session.commit()
    assert status == "new"
