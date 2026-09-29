"""Места компании (решения владельца продукта 28.09):
активных учёток не больше мест (П-2д); set-seats предупреждает, если
новый пул меньше потраченного (П-2г)."""

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.cli import _parser
from corp_ed.core.security import hash_password
from corp_ed.domain.models import Tenant, User, UserRole
from corp_ed.repositories.audit_repository import AuditRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services.seats import seats_check
from corp_ed.services.tenant_service import TenantService
from tests.api.conftest import bearer
from tests.test_credits import spend


async def _fill_seats(session: AsyncSession, tenant: Tenant, seats: int) -> None:
    """Компания на seats мест, все места уже заняты активными учётками."""
    tenant.seats = seats
    session.add(tenant)
    await session.commit()
    active = await session.scalar(
        select(func.count())
        .select_from(User)
        .where(User.tenant_id == tenant.id, User.is_active.is_(True))
    )
    for n in range(seats - int(active or 0)):
        session.add(
            User(
                id=uuid4(),
                tenant_id=tenant.id,
                email=f"filler{n}@test.com",
                role=UserRole.EMPLOYEE,
                hashed_password=hash_password("x" * 12),
            )
        )
    await session.commit()


# --- П-2д: активных учёток не больше мест -------------------------------------------


async def test_admin_cannot_add_user_beyond_seats(
    api: httpx.AsyncClient,
    admin_account: User,
    tenant_ctx: Tenant,
    session: AsyncSession,
) -> None:
    await _fill_seats(session, tenant_ctx, 3)

    response = await api.post(
        "/api/v1/users",
        json={"email": "extra@test.com", "role": "employee"},
        headers=bearer(admin_account),
    )

    assert response.status_code == 409
    assert "Все места заняты: активных сотрудников 3 из 3" in response.json()["detail"]


async def test_blocked_users_free_their_seats(
    api: httpx.AsyncClient,
    admin_account: User,
    account: User,
    tenant_ctx: Tenant,
    session: AsyncSession,
) -> None:
    await _fill_seats(session, tenant_ctx, 3)
    block = await api.patch(
        f"/api/v1/users/{account.id}",
        json={"is_active": False},
        headers=bearer(admin_account),
    )
    assert block.status_code == 200

    created = await api.post(
        "/api/v1/users",
        json={"email": "newcomer@test.com", "role": "employee"},
        headers=bearer(admin_account),
    )
    assert created.status_code == 201

    # Место снова занято — разблокировать прежнего нельзя.
    unblock = await api.patch(
        f"/api/v1/users/{account.id}",
        json={"is_active": True},
        headers=bearer(admin_account),
    )
    assert unblock.status_code == 409


async def test_invite_join_stops_when_seats_are_full(
    api: httpx.AsyncClient,
    admin_account: User,
    tenant_ctx: Tenant,
    session: AsyncSession,
) -> None:
    created = await api.post(
        "/api/v1/invites", json={"max_uses": 10}, headers=bearer(admin_account)
    )
    token = created.json()["token"]
    await _fill_seats(session, tenant_ctx, 2)

    response = await api.post(
        "/api/v1/invites/accept",
        json={
            "company_code": "test",
            "token": token,
            "email": "late@test.com",
            "password": "длинная фраза для входа",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "В компании закончились места — сообщите администратору."
    )


# --- П-2г: предупреждение set-seats -------------------------------------------------


async def test_seats_check_warns_when_new_pool_is_spent(
    session: AsyncSession, tenant_ctx: Tenant, admin_account: User
) -> None:
    spend(session, admin_account, 500)
    await session.commit()

    fine = await seats_check(session, "test", 2)
    assert (fine.stops_pool, fine.message) == (False, None)

    cut = await seats_check(session, "TEST", 1)
    assert cut.stops_pool is True
    assert cut.message is not None
    assert "потрачено 500 кредитов, новый пул — 420 (1 × 420)" in cut.message
    assert "остановятся до" in cut.message


async def test_seats_check_mentions_extra_active_users(
    session: AsyncSession, tenant_ctx: Tenant, admin_account: User, account: User
) -> None:
    check = await seats_check(session, "test", 1)

    assert check.stops_pool is False
    assert check.message is not None
    assert "активных учёток 2 — больше мест" in check.message


async def test_seats_check_for_unknown_company_is_silent(session: AsyncSession) -> None:
    assert (await seats_check(session, "nobody", 5)).message is None


def test_set_seats_has_confirmation_flag() -> None:
    args = _parser().parse_args(["set-seats", "--code", "acme", "--seats", "10"])
    assert args.yes is False
    confirmed = _parser().parse_args(
        ["set-seats", "--code", "acme", "--seats", "10", "--yes"]
    )
    assert confirmed.yes is True


@pytest.mark.parametrize("seats", [1, 30])
async def test_first_admin_always_fits(session: AsyncSession, seats: int) -> None:
    """Компания создаётся с администратором — места ≥ 1, он всегда влезает."""
    result = await TenantService(
        TenantRepository(session),
        UserRepository(session),
        AuditRepository(session),
        session,
    ).provision(
        company_code=f"fit{seats}",
        name="F",
        admin_email="admin@fit.ru",
        admin_full_name=None,
        admin_password="Temp-first-login-2026",
        seats=seats,
    )
    assert result.admin.is_active is True
