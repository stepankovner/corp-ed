"""Пул кредитов через HTTP: 402 при исчерпании и отчёт о расходе для админа."""

from uuid import uuid4

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import QaLog, Tenant, User, UserRole
from corp_ed.llm.fake import FakeAdapter
from tests.api.conftest import bearer
from tests.test_credits import spend


async def _exhaust(session: AsyncSession, tenant: Tenant, user: User) -> None:
    tenant.seats = 1
    spend(session, user, 420)
    await session.commit()


async def _ask(api: httpx.AsyncClient, user: User) -> httpx.Response:
    return await api.post(
        "/api/v1/faq/ask", json={"question": "Сколько отпуска?"}, headers=bearer(user)
    )


# --- жёсткая остановка ------------------------------------------------------------


async def test_exhausted_pool_is_402_with_code(
    api: httpx.AsyncClient,
    account: User,
    tenant_ctx: Tenant,
    session: AsyncSession,
    fake_llm: FakeAdapter,
) -> None:
    await _exhaust(session, tenant_ctx, account)

    response = await _ask(api, account)

    assert response.status_code == 402
    assert response.json()["code"] == "credits_exhausted"
    assert "администратору" in response.json()["detail"]
    assert fake_llm.calls == []


async def test_admin_is_stopped_too(
    api: httpx.AsyncClient,
    admin_account: User,
    tenant_ctx: Tenant,
    session: AsyncSession,
) -> None:
    """Пул общий: у администратора нет обхода."""
    await _exhaust(session, tenant_ctx, admin_account)
    assert (await _ask(api, admin_account)).status_code == 402


async def test_other_company_exhaustion_does_not_stop_us(
    api: httpx.AsyncClient,
    account: User,
    tenant_ctx: Tenant,
    session: AsyncSession,
) -> None:
    other = Tenant(id=uuid4(), company_code="other", name="Other", seats=1)
    session.add(other)
    await session.commit()
    with tenant_scope(other.id):
        stranger = User(
            tenant_id=other.id,
            email="s@o.ru",
            role=UserRole.EMPLOYEE,
            hashed_password="x",
        )
        session.add(stranger)
        await session.commit()
        spend(session, stranger, 420)
        await session.commit()

    assert (await _ask(api, account)).status_code == 200
    assert (await _ask(api, stranger)).status_code == 402


async def test_rejected_question_is_not_logged(
    api: httpx.AsyncClient,
    account: User,
    tenant_ctx: Tenant,
    session: AsyncSession,
) -> None:
    await _exhaust(session, tenant_ctx, account)

    await _ask(api, account)

    with tenant_scope(tenant_ctx.id):
        count = await session.scalar(select(func.count()).select_from(QaLog))
        await session.commit()
    assert count == 1  # только запись из _exhaust


# --- отчёт о расходе --------------------------------------------------------------


async def test_admin_sees_usage(
    api: httpx.AsyncClient,
    admin_account: User,
    account: User,
    tenant_ctx: Tenant,
    session: AsyncSession,
) -> None:
    spend(session, account, 7)
    await session.commit()

    response = await api.get("/api/v1/usage", headers=bearer(admin_account))

    assert response.status_code == 200
    body = response.json()
    assert body["seats"] == 30
    assert body["credits_per_seat"] == 420
    assert body["pool"] == 12_600
    assert body["used"] == 7
    assert body["remaining"] == 12_593
    assert body["exhausted"] is False
    assert body["period_start"] < body["period_end"]


async def test_usage_counts_answers(
    api: httpx.AsyncClient, admin_account: User, account: User
) -> None:
    await _ask(api, account)
    await _ask(api, account)

    body = (await api.get("/api/v1/usage", headers=bearer(admin_account))).json()

    assert body["used"] == 2


async def test_employee_cannot_see_usage(api: httpx.AsyncClient, account: User) -> None:
    response = await api.get("/api/v1/usage", headers=bearer(account))
    assert response.status_code == 403


async def test_anonymous_cannot_see_usage(api: httpx.AsyncClient) -> None:
    assert (await api.get("/api/v1/usage")).status_code == 401


async def test_usage_has_no_tenant_parameter(
    api: httpx.AsyncClient,
    admin_account: User,
    session: AsyncSession,
) -> None:
    """Чужой пул не запросить: компания только из токена, лишний
    параметр игнорируется."""
    other = Tenant(id=uuid4(), company_code="other", name="Other", seats=99)
    session.add(other)
    await session.commit()

    response = await api.get(
        "/api/v1/usage",
        params={"tenant_id": str(other.id)},
        headers=bearer(admin_account),
    )

    assert response.json()["seats"] == 30
