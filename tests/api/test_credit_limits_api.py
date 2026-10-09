"""Когда кредитов нет (решения владельца 09.10): сотрудник просит
администратора пополнить — одно уведомление на эпизод исчерпания; личный
дневной лимит — отдельная остановка со своим кодом."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.config import BillingSettings
from corp_ed.core.exceptions import DailyLimitExhaustedError
from corp_ed.domain.credits import billing_day
from corp_ed.domain.models import (
    AuditEvent,
    CreditGrant,
    Notification,
    Tenant,
    User,
)
from corp_ed.llm.fake import FakeAdapter
from tests.api.conftest import bearer
from tests.conftest import make_credit_service
from tests.test_credits import spend

TOPUP = "/api/v1/credits/topup-request"


async def _exhaust(session: AsyncSession, tenant: Tenant, user: User) -> None:
    tenant.seats = 1
    spend(session, user, 420)
    await session.commit()


async def _topup_notices(session: AsyncSession) -> list[Notification]:
    return list(
        (
            await session.scalars(
                select(Notification).where(
                    Notification.kind == "credits_topup_requested"
                )
            )
        ).all()
    )


# --- «Попросить администратора пополнить» ----------------------------------------


async def test_request_only_when_questions_are_stopped(
    api: httpx.AsyncClient, employee: User
) -> None:
    state = await api.get(TOPUP, headers=bearer(employee))
    assert state.json() == {"stopped": False, "requested": False}

    response = await api.post(TOPUP, headers=bearer(employee))

    assert response.status_code == 409
    assert response.json()["code"] == "credits_available"


async def test_admins_get_one_notice_per_episode(
    api: httpx.AsyncClient,
    session: AsyncSession,
    tenant_ctx: Tenant,
    admin: User,
    employee: User,
    account: User,
) -> None:
    await _exhaust(session, tenant_ctx, employee)

    first = await api.post(TOPUP, headers=bearer(employee))
    second = await api.post(TOPUP, headers=bearer(account))

    assert first.status_code == 200, first.text
    assert first.json() == {"sent": True}
    assert second.json() == {"sent": False}
    # Остальные видят «Администратор уже уведомлён».
    state = await api.get(TOPUP, headers=bearer(account))
    assert state.json() == {"stopped": True, "requested": True}
    [notice] = await _topup_notices(session)
    assert notice.user_id == admin.id
    assert notice.title == "Сотрудники просят пополнить кредиты"
    assert notice.link == "/admin/tariff"
    events = (
        await session.scalars(
            select(AuditEvent).where(AuditEvent.action == "credits.topup_requested")
        )
    ).all()
    assert len(events) == 1

    # Пополнили — эпизод закончился; кончились снова — можно снова одно.
    session.add(
        CreditGrant(
            tenant_id=tenant_ctx.id,
            credits=1,
            remaining=0,
            source="manual",
            expires_at=datetime.now(UTC) + timedelta(days=365),
        )
    )
    await session.commit()
    assert (await api.get(TOPUP, headers=bearer(account))).json()["requested"] is False
    again = await api.post(TOPUP, headers=bearer(account))
    assert again.json() == {"sent": True}
    assert len(await _topup_notices(session)) == 2


async def test_two_parallel_presses_send_one_notice(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    admin: User,
    employee: User,
    account: User,
) -> None:
    await _exhaust(session, tenant_ctx, employee)

    async def press(user: User) -> bool:
        async with session_maker() as own:
            return await make_credit_service(own).request_topup(user)

    results = await asyncio.gather(press(employee), press(account))

    assert sorted(results) == [False, True]
    assert len(await _topup_notices(session)) == 1


# --- личный дневной лимит ---------------------------------------------------------


async def _daily(session: AsyncSession, tenant: Tenant, limit: int | None) -> None:
    tenant.daily_credits_per_member = limit
    await session.commit()


async def test_daily_limit_stops_only_who_spent_it(
    session: AsyncSession,
    tenant_ctx: Tenant,
    admin: User,
    employee: User,
    account: User,
) -> None:
    await _daily(session, tenant_ctx, 5)
    service = make_credit_service(session)
    spend(session, employee, 5)
    spend(session, admin, 5)
    await session.commit()

    with pytest.raises(DailyLimitExhaustedError):
        await service.ensure_available(employee)
    # Администратор — тоже: лимит действует на всех.
    with pytest.raises(DailyLimitExhaustedError):
        await service.ensure_available(admin)
    await service.ensure_available(account)


async def test_daily_limit_counts_only_today(
    session: AsyncSession, tenant_ctx: Tenant, employee: User
) -> None:
    await _daily(session, tenant_ctx, 5)
    start, _ = billing_day(datetime.now(UTC), BillingSettings().zone)
    spend(session, employee, 100, created_at=start - timedelta(minutes=1))
    spend(session, employee, 4)
    await session.commit()

    await make_credit_service(session).ensure_available(employee)


async def test_no_daily_limit_by_default(
    session: AsyncSession, tenant_ctx: Tenant, employee: User
) -> None:
    assert tenant_ctx.daily_credits_per_member is None
    spend(session, employee, 400)
    await session.commit()

    await make_credit_service(session).ensure_available(employee)


async def test_daily_limit_has_its_own_code_and_text(
    api: httpx.AsyncClient,
    session: AsyncSession,
    tenant_ctx: Tenant,
    account: User,
    fake_llm: FakeAdapter,
) -> None:
    await _daily(session, tenant_ctx, 3)
    spend(session, account, 3)
    await session.commit()

    response = await api.post(
        "/api/v1/faq/ask",
        json={"question": "Сколько отпуска?"},
        headers=bearer(account),
    )

    assert response.status_code == 429
    assert response.json() == {
        "detail": "Ваш дневной лимит на сегодня исчерпан, он обновится завтра. "
        "Лимит задаёт администратор компании",
        "code": "daily_limit_exhausted",
    }
    assert int(response.headers["Retry-After"]) > 0
    assert fake_llm.calls == []


async def test_admin_sets_and_removes_daily_limit(
    api: httpx.AsyncClient,
    session: AsyncSession,
    admin: User,
    employee: User,
) -> None:
    headers = bearer(admin)
    assert (await api.get("/api/v1/company", headers=headers)).json()[
        "daily_credits_per_member"
    ] is None

    response = await api.patch(
        "/api/v1/company", json={"daily_credits_per_member": 15}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["daily_credits_per_member"] == 15
    # Не прислали — не меняется; null — лимит снят.
    kept = await api.patch("/api/v1/company", json={"name": "Тест"}, headers=headers)
    assert kept.json()["daily_credits_per_member"] == 15
    removed = await api.patch(
        "/api/v1/company", json={"daily_credits_per_member": None}, headers=headers
    )
    assert removed.json()["daily_credits_per_member"] is None

    events = (
        await session.scalars(
            select(AuditEvent).where(AuditEvent.action == "tenant.settings_updated")
        )
    ).all()
    changes = [e.details.get("daily_credits_per_member") for e in events]
    assert {"old": None, "new": 15} in changes
    assert {"old": 15, "new": None} in changes

    assert (
        await api.patch(
            "/api/v1/company", json={"daily_credits_per_member": 0}, headers=headers
        )
    ).status_code == 422
    assert (
        await api.patch(
            "/api/v1/company",
            json={"daily_credits_per_member": 5},
            headers=bearer(employee),
        )
    ).status_code == 403
