"""Приглашения: админ создаёт ссылку и код, человек со своей учёткой
вступает в компанию (решения 28.09 и 03.10, ТЗ §2)."""

import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.security import hash_refresh_token
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import (
    Account,
    AuditEvent,
    Invite,
    InviteLookup,
    MemberStatus,
    Tenant,
    User,
    UserRole,
)
from tests.api.conftest import account_bearer, bearer
from tests.factories import make_account, make_user


async def _create(
    api: httpx.AsyncClient, admin: User, **body: object
) -> dict[str, object]:
    response = await api.post("/api/v1/invites", json=body, headers=bearer(admin))
    assert response.status_code == 201, response.text
    return response.json()


async def _joiner(session: AsyncSession, email: str = "new@test.com") -> Account:
    """Человек с подтверждённой учёткой kronto и без компании."""
    account = make_account(email, full_name="Новый Сотрудник")
    session.add(account)
    await session.commit()
    return account


async def _accept(
    api: httpx.AsyncClient, account: Account, secret: object
) -> httpx.Response:
    return await api.post(
        "/api/v1/invites/accept",
        json={"secret": secret},
        headers=account_bearer(account),
    )


async def _invite(session: AsyncSession, tenant: Tenant) -> Invite:
    with tenant_scope(tenant.id):
        invite = (await session.scalars(select(Invite))).one()
        await session.refresh(invite)
        return invite


# --- администратор -------------------------------------------------------------


async def test_admin_creates_link_and_code_shown_once(
    api: httpx.AsyncClient,
    admin_account: User,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    created = await _create(api, admin_account)

    token, code = str(created["token"]), str(created["code"])
    assert len(token) >= 40
    assert re.fullmatch(r"[0-9A-Z]{4}-[0-9A-Z]{4}", code)
    invite = await _invite(session, tenant_ctx)
    assert invite.token_hash == hash_refresh_token(token)
    assert invite.code_hash and code.replace("-", "") not in invite.code_hash
    assert invite.max_uses == tenant_ctx.seats
    lookups = (await session.scalars(select(InviteLookup))).all()
    assert {lookup.hash for lookup in lookups} == {invite.token_hash, invite.code_hash}
    events = (await session.scalars(select(AuditEvent.action))).all()
    assert "invite.created" in events

    listed = await api.get("/api/v1/invites", headers=bearer(admin_account))
    assert listed.status_code == 200
    [item] = listed.json()
    assert "token" not in item and "code" not in item
    assert item["status"] == "active"


async def test_admin_sets_limits(api: httpx.AsyncClient, admin_account: User) -> None:
    created = await _create(
        api,
        admin_account,
        ttl_days=1,
        max_uses=3,
        email_domain="@Acme.RU",
        requires_approval=True,
    )
    invite = created["invite"]
    assert isinstance(invite, dict)
    assert invite["max_uses"] == 3
    assert invite["email_domain"] == "acme.ru"
    assert invite["requires_approval"] is True


@pytest.mark.parametrize(
    "body",
    [
        {"ttl_days": 0},
        {"ttl_days": 31},
        {"max_uses": 0},
        {"email_domain": "not a domain"},
        {"role": "admin"},
    ],
)
async def test_bad_invite_settings_are_rejected(
    api: httpx.AsyncClient, admin_account: User, body: dict[str, object]
) -> None:
    """В том числе роль: по приглашению админа вступают только сотрудники."""
    response = await api.post(
        "/api/v1/invites", json=body, headers=bearer(admin_account)
    )
    assert response.status_code == 422


async def test_employee_and_anonymous_cannot_manage_invites(
    api: httpx.AsyncClient, account: User
) -> None:
    headers = bearer(account)
    assert (
        await api.post("/api/v1/invites", json={}, headers=headers)
    ).status_code == 403
    assert (await api.get("/api/v1/invites", headers=headers)).status_code == 403
    assert (await api.get("/api/v1/invites")).status_code == 401


async def test_admin_cannot_revoke_other_company_invite(
    api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    other = Tenant(id=uuid4(), company_code="other", name="Other")
    session.add(other)
    await session.commit()
    with tenant_scope(other.id):
        invite = Invite(
            token_hash=hash_refresh_token("x" * 43),
            expires_at=datetime.now(UTC) + timedelta(days=1),
            max_uses=1,
        )
        session.add(invite)
        await session.commit()

    response = await api.delete(
        f"/api/v1/invites/{invite.id}", headers=bearer(admin_account)
    )
    assert response.status_code == 404


# --- человек с приглашением ----------------------------------------------------


async def test_preview_by_link_and_by_code(
    api: httpx.AsyncClient, admin_account: User
) -> None:
    created = await _create(api, admin_account, requires_approval=True)
    code = str(created["code"])
    # Код, продиктованный по телефону: регистр, пробелы, без дефиса.
    spoken = code.lower().replace("-", " ")

    for secret in (created["token"], code, spoken):
        response = await api.post("/api/v1/invites/preview", json={"secret": secret})
        assert response.status_code == 200, secret
        assert response.json()["company_name"] == "Test Co"
        assert response.json()["requires_approval"] is True


@pytest.mark.parametrize("form", ["token", "code"])
async def test_join_by_link_or_code_switches_the_session(
    api: httpx.AsyncClient,
    admin_account: User,
    session: AsyncSession,
    tenant_ctx: Tenant,
    form: str,
) -> None:
    created = await _create(api, admin_account)
    joiner = await _joiner(session)

    response = await _accept(api, joiner, created[form])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "joined"
    assert body["company_name"] == "Test Co"
    headers = {"Authorization": f"Bearer {body['session']['access_token']}"}
    me = (await api.get("/api/v1/auth/me", headers=headers)).json()
    assert me["company"]["name"] == "Test Co"
    assert me["company"]["role"] == "employee"
    assert (await _invite(session, tenant_ctx)).uses == 1
    events = (await session.scalars(select(AuditEvent.action))).all()
    assert "user.joined_by_invite" in events


async def test_already_member_does_not_use_the_invite(
    api: httpx.AsyncClient,
    admin_account: User,
    account: User,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    created = await _create(api, admin_account)
    assert account.account is not None

    response = await _accept(api, account.account, created["token"])

    assert response.json()["outcome"] == "already_member"
    assert (await _invite(session, tenant_ctx)).uses == 0


async def test_invite_with_approval_waits_for_admin(
    api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    created = await _create(api, admin_account, requires_approval=True)
    joiner = await _joiner(session)

    response = await _accept(api, joiner, created["code"])

    assert response.json()["outcome"] == "pending"
    assert response.json()["session"] is None
    listed = await api.get("/api/v1/users", headers=bearer(admin_account))
    [waiting] = [u for u in listed.json() if u["email"] == joiner.email]
    assert waiting["status"] == "pending"

    approved = await api.post(
        f"/api/v1/users/{waiting['id']}/approve", headers=bearer(admin_account)
    )
    assert approved.status_code == 200
    switched = await api.post(
        "/api/v1/auth/switch-company",
        json={"tenant_id": str(admin_account.tenant_id)},
        headers=account_bearer(joiner),
    )
    assert switched.status_code == 200


async def test_invite_stops_after_max_uses(
    api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    created = await _create(api, admin_account, max_uses=1)
    first = await _joiner(session, "one@test.com")
    second = await _joiner(session, "two@test.com")

    assert (await _accept(api, first, created["token"])).status_code == 200
    response = await _accept(api, second, created["token"])
    assert response.status_code == 404
    assert response.json()["code"] == "invalid_invite"


async def test_revoked_and_expired_invites_do_not_work(
    api: httpx.AsyncClient,
    admin_account: User,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    revoked = await _create(api, admin_account)
    invite_id = revoked["invite"]["id"]  # type: ignore[index]
    response = await api.delete(
        f"/api/v1/invites/{invite_id}", headers=bearer(admin_account)
    )
    assert response.status_code == 204
    joiner = await _joiner(session)
    for secret in (revoked["token"], revoked["code"]):
        assert (await _accept(api, joiner, secret)).status_code == 404
        preview = await api.post("/api/v1/invites/preview", json={"secret": secret})
        assert preview.status_code == 404

    expired = await _create(api, admin_account)
    with tenant_scope(tenant_ctx.id):
        invite = await session.get(Invite, expired["invite"]["id"])  # type: ignore[index]
        assert invite is not None
        invite.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()
    assert (await _accept(api, joiner, expired["token"])).status_code == 404


async def test_unknown_secret_and_inactive_company(
    api: httpx.AsyncClient,
    admin_account: User,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    joiner = await _joiner(session)
    assert (await _accept(api, joiner, "A" * 43)).status_code == 404
    assert (await _accept(api, joiner, "ZZZZ-ZZZZ")).status_code == 404

    created = await _create(api, admin_account)
    tenant_ctx.is_active = False
    await session.commit()
    assert (await _accept(api, joiner, created["token"])).status_code == 404


async def test_email_domain_restriction(
    api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    created = await _create(api, admin_account, email_domain="acme.ru")
    outsider = await _joiner(session, "anna@gmail.com")
    insider = await _joiner(session, "anna@msk.acme.ru")

    refused = await _accept(api, outsider, created["token"])
    assert refused.status_code == 422
    assert "@acme.ru" in refused.json()["detail"]
    assert (await _accept(api, insider, created["token"])).status_code == 200


async def test_blocked_member_cannot_rejoin_by_invite(
    api: httpx.AsyncClient, admin_account: User, account: User, session: AsyncSession
) -> None:
    account.status = MemberStatus.BLOCKED
    await session.commit()
    created = await _create(api, admin_account)
    assert account.account is not None

    response = await _accept(api, account.account, created["token"])
    assert response.status_code == 403
    assert response.json()["code"] == "membership_blocked"


async def test_left_member_returns_with_invite_role(
    api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    # Ушёл давно: purge уже стёр его данные в компании.
    former = make_user(
        email="former@test.com",
        role=UserRole.ADMIN,
        status=MemberStatus.LEFT,
        left_at=datetime.now(UTC) - timedelta(days=60),
        data_purged_at=datetime.now(UTC) - timedelta(days=30),
    )
    session.add(former)
    await session.commit()
    created = await _create(api, admin_account)
    assert former.account is not None

    response = await _accept(api, former.account, created["token"])

    assert response.json()["outcome"] == "joined"
    await session.refresh(former)
    assert former.status is MemberStatus.ACTIVE
    assert former.role is UserRole.EMPLOYEE
    assert former.left_at is None
    # Вернулся — следующий уход снова отсчитает 30 дней до очистки.
    assert former.data_purged_at is None


async def test_join_needs_a_free_seat(
    api: httpx.AsyncClient,
    admin_account: User,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    created = await _create(api, admin_account)
    tenant_ctx.seats = 1
    await session.commit()
    joiner = await _joiner(session)

    response = await _accept(api, joiner, created["token"])
    assert response.status_code == 409


async def test_accept_requires_a_signed_in_account(
    api: httpx.AsyncClient, admin_account: User
) -> None:
    created = await _create(api, admin_account)
    response = await api.post(
        "/api/v1/invites/accept", json={"secret": created["token"]}
    )
    assert response.status_code == 401


async def test_public_endpoints_are_rate_limited(api: httpx.AsyncClient) -> None:
    """Перебор кодов упирается в лимит по IP."""
    statuses = [
        (
            await api.post("/api/v1/invites/preview", json={"secret": f"AAAA-{i:04d}"})
        ).status_code
        for i in range(61)
    ]
    assert statuses[:60] == [404] * 60
    assert statuses[60] == 429
