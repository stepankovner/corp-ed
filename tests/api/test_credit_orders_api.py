"""Пакеты кредитов (решение владельца 09.10): администратор заказывает
пакет, команда kronto отмечает оплату по счёту в нашей панели — кредиты
зачисляются на 12 месяцев; ручное начисление с комментарием."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.api.v1.dependencies import get_team_notifier
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import (
    AuditEvent,
    CreditGrant,
    Notification,
    StaffMember,
    Tenant,
    User,
    UserRole,
)
from corp_ed.main import app
from tests.api.conftest import bearer
from tests.factories import make_user
from tests.team_notify_helpers import RecordingNotifier

ORDERS = "/api/v1/credits/orders"


@pytest.fixture
def notifier() -> Iterator[list[str]]:
    sent: list[str] = []
    app.dependency_overrides[get_team_notifier] = lambda: RecordingNotifier(sent)
    yield sent
    app.dependency_overrides.pop(get_team_notifier, None)


@pytest.fixture
async def staff(session: AsyncSession, admin: User) -> User:
    """Администратор тестовой компании, он же — команда kronto."""
    assert admin.account is not None
    session.add(StaffMember(account_id=admin.account.id))
    await session.commit()
    return admin


async def _events(session: AsyncSession, action: str) -> list[AuditEvent]:
    return list(
        (
            await session.scalars(select(AuditEvent).where(AuditEvent.action == action))
        ).all()
    )


async def _order(api: httpx.AsyncClient, admin: User, pack: str) -> dict[str, object]:
    response = await api.post(ORDERS, json={"pack": pack}, headers=bearer(admin))
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


# --- администратор компании -------------------------------------------------------


async def test_packs_come_from_the_backend(api: httpx.AsyncClient, admin: User) -> None:
    response = await api.get("/api/v1/credits/packs", headers=bearer(admin))

    assert response.status_code == 200
    assert response.json() == [
        {"code": "pack_500", "credits": 500, "price_kopecks": 149_000},
        {"code": "pack_2000", "credits": 2_000, "price_kopecks": 549_000},
        {"code": "pack_5000", "credits": 5_000, "price_kopecks": 1_299_000},
    ]


async def test_admin_orders_a_pack_and_the_team_is_told(
    api: httpx.AsyncClient,
    session: AsyncSession,
    admin: User,
    notifier: list[str],
) -> None:
    first = await _order(api, admin, "pack_2000")
    second = await _order(api, admin, "pack_500")

    assert first["number"] == 1
    assert second["number"] == 2
    assert first["status"] == "awaiting_payment"
    assert first["payment_method"] == "invoice"
    assert (first["credits"], first["amount_kopecks"]) == (2_000, 549_000)
    listed = (await api.get(ORDERS, headers=bearer(admin))).json()
    assert [item["number"] for item in listed] == [2, 1]
    [event, _] = await _events(session, "credits.order_created")
    assert event.details["credits"] in (2_000, 500)
    # Команде — без персональных данных: код компании, номер, числа.
    assert len(notifier) == 2
    assert "test-1" in notifier[0]
    assert "2 000 кредитов" in notifier[0]
    assert "admin@test.com" not in notifier[0]


async def test_orders_are_for_admins_only(
    api: httpx.AsyncClient, employee: User
) -> None:
    assert (
        await api.post(ORDERS, json={"pack": "pack_500"}, headers=bearer(employee))
    ).status_code == 403
    assert (await api.get(ORDERS, headers=bearer(employee))).status_code == 403


async def test_unknown_pack_is_refused(api: httpx.AsyncClient, admin: User) -> None:
    response = await api.post(ORDERS, json={"pack": "gold"}, headers=bearer(admin))
    assert response.status_code == 409
    assert response.json()["code"] == "unknown_pack"


async def test_open_orders_are_limited(api: httpx.AsyncClient, admin: User) -> None:
    for _ in range(5):
        await _order(api, admin, "pack_500")
    response = await api.post(ORDERS, json={"pack": "pack_500"}, headers=bearer(admin))
    assert response.status_code == 409
    assert response.json()["code"] == "too_many_orders"


async def test_company_does_not_see_other_company_orders(
    api: httpx.AsyncClient, session: AsyncSession, admin: User
) -> None:
    await _order(api, admin, "pack_500")
    other = Tenant(id=uuid4(), company_code="other", name="Other")
    session.add(other)
    await session.commit()
    with tenant_scope(other.id):
        stranger = make_user(
            tenant_id=other.id,
            email="boss@other.ru",
            role=UserRole.ADMIN,
            hashed_password="x",
        )
        assert stranger.account is not None
        stranger.account.totp_enabled_at = datetime.now(UTC)
        session.add(stranger)
        await session.commit()

    assert (await api.get(ORDERS, headers=bearer(stranger))).json() == []


# --- наша панель --------------------------------------------------------------------


async def test_team_marks_order_paid_and_credits_arrive(
    api: httpx.AsyncClient,
    session: AsyncSession,
    staff: User,
    tenant_ctx: Tenant,
) -> None:
    order = await _order(api, staff, "pack_500")

    listed = await api.get("/api/v1/staff/credit-orders", headers=bearer(staff))
    assert listed.status_code == 200, listed.text
    [row] = listed.json()
    assert (row["company_code"], row["number"], row["status"]) == (
        "test",
        1,
        "awaiting_payment",
    )

    paid_url = (
        f"/api/v1/staff/companies/{tenant_ctx.id}/credit-orders/{order['id']}/paid"
    )
    response = await api.post(paid_url, headers=bearer(staff))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "paid"

    [grant] = (await session.scalars(select(CreditGrant))).all()
    assert (grant.source, grant.credits, grant.remaining) == ("purchase", 500, 500)
    assert grant.expires_at - datetime.now(UTC) > timedelta(days=360)
    [event] = await _events(session, "credits.order_paid")
    assert staff.account is not None
    assert event.details["staff_account_id"] == str(staff.account.id)
    [notice] = (
        await session.scalars(
            select(Notification).where(Notification.kind == "credits_added")
        )
    ).all()
    assert notice.title == "Кредиты зачислены"
    assert "500" in notice.body
    usage = (await api.get("/api/v1/usage", headers=bearer(staff))).json()
    assert usage["purchased"] == 500

    # Второй раз не зачислит; оплаченный не отменить.
    again = await api.post(paid_url, headers=bearer(staff))
    assert again.status_code == 409
    cancel = await api.post(paid_url.replace("/paid", "/cancel"), headers=bearer(staff))
    assert cancel.status_code == 409
    assert len((await session.scalars(select(CreditGrant))).all()) == 1


async def test_team_cancels_unpaid_order(
    api: httpx.AsyncClient,
    session: AsyncSession,
    staff: User,
    tenant_ctx: Tenant,
) -> None:
    order = await _order(api, staff, "pack_500")
    url = f"/api/v1/staff/companies/{tenant_ctx.id}/credit-orders/{order['id']}"

    response = await api.post(f"{url}/cancel", headers=bearer(staff))

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert (await api.post(f"{url}/paid", headers=bearer(staff))).status_code == 409
    assert len(await _events(session, "credits.order_cancelled")) == 1
    awaiting = await api.get("/api/v1/staff/credit-orders", headers=bearer(staff))
    assert awaiting.json() == []
    everything = await api.get(
        "/api/v1/staff/credit-orders?status=all", headers=bearer(staff)
    )
    assert [o["status"] for o in everything.json()] == ["cancelled"]


async def test_team_grants_credits_with_a_comment(
    api: httpx.AsyncClient,
    session: AsyncSession,
    staff: User,
    tenant_ctx: Tenant,
) -> None:
    response = await api.post(
        f"/api/v1/staff/companies/{tenant_ctx.id}/credits",
        json={"credits": 300, "comment": "Компенсация за сбой 08.10"},
        headers=bearer(staff),
    )

    assert response.status_code == 201, response.text
    [grant] = (await session.scalars(select(CreditGrant))).all()
    assert (grant.source, grant.credits, grant.comment) == (
        "manual",
        300,
        "Компенсация за сбой 08.10",
    )
    [event] = await _events(session, "credits.granted")
    assert event.details["comment"] == "Компенсация за сбой 08.10"
    company = await api.get(
        f"/api/v1/staff/companies/{tenant_ctx.id}", headers=bearer(staff)
    )
    assert company.json()["purchased_credits"] == 300


async def test_grant_needs_a_comment(
    api: httpx.AsyncClient, staff: User, tenant_ctx: Tenant
) -> None:
    response = await api.post(
        f"/api/v1/staff/companies/{tenant_ctx.id}/credits",
        json={"credits": 300, "comment": " "},
        headers=bearer(staff),
    )
    assert response.status_code == 422


async def test_credit_actions_are_hidden_from_non_staff(
    api: httpx.AsyncClient, admin: User, tenant_ctx: Tenant
) -> None:
    for method, url in (
        ("GET", "/api/v1/staff/credit-orders"),
        ("POST", f"/api/v1/staff/companies/{tenant_ctx.id}/credits"),
        (
            "POST",
            f"/api/v1/staff/companies/{tenant_ctx.id}/credit-orders/{uuid4()}/paid",
        ),
    ):
        response = await api.request(
            method, url, json={"credits": 1, "comment": "x"}, headers=bearer(admin)
        )
        assert response.status_code == 404, url
