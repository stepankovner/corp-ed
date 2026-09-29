"""Ссылки-приглашения: админ создаёт, человек по ссылке заводит учётку
сотрудника и сразу входит (решение владельца продукта 28.09)."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.security import hash_refresh_token
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import AuditEvent, Invite, Tenant, User, UserRole
from tests.api.conftest import bearer

NEW_PASSWORD = "длинная фраза для входа"


async def _create(
    api: httpx.AsyncClient, admin: User, **body: object
) -> dict[str, object]:
    response = await api.post("/api/v1/invites", json=body, headers=bearer(admin))
    assert response.status_code == 201, response.text
    return response.json()


async def _accept(
    api: httpx.AsyncClient,
    token: object,
    email: str = "new@test.com",
    *,
    company: str = "test",
    password: str = NEW_PASSWORD,
) -> httpx.Response:
    return await api.post(
        "/api/v1/invites/accept",
        json={
            "company_code": company,
            "token": token,
            "email": email,
            "full_name": "Новый Сотрудник",
            "password": password,
        },
    )


async def _invite(session: AsyncSession, tenant: Tenant) -> Invite:
    tenant_id = tenant.id
    with tenant_scope(tenant_id):
        invite = (await session.scalars(select(Invite))).one()
        await session.refresh(invite)
        return invite


# --- администратор -------------------------------------------------------------


async def test_admin_creates_link_shown_once(
    api: httpx.AsyncClient,
    admin_account: User,
    tenant_ctx: Tenant,
    session: AsyncSession,
) -> None:
    created = await _create(api, admin_account)

    token = created["token"]
    assert isinstance(token, str) and len(token) >= 40
    assert created["company_code"] == "test"
    invite = created["invite"]
    assert isinstance(invite, dict)
    assert invite["status"] == "active"
    # По умолчанию — одна ссылка на всю команду: по числу мест.
    assert (invite["max_uses"], invite["uses"]) == (30, 0)
    expires = datetime.fromisoformat(str(invite["expires_at"]))
    assert (
        timedelta(days=6, hours=23) < expires - datetime.now(UTC) <= timedelta(days=7)
    )

    stored = await _invite(session, tenant_ctx)
    assert stored.token_hash == hash_refresh_token(token)
    assert token not in stored.token_hash

    listed = await api.get("/api/v1/invites", headers=bearer(admin_account))
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [invite["id"]]
    assert "token" not in listed.json()[0]

    [event] = (
        await session.scalars(
            select(AuditEvent).where(AuditEvent.action == "invite.created")
        )
    ).all()
    assert event.details["max_uses"] == 30


async def test_admin_sets_limits(api: httpx.AsyncClient, admin_account: User) -> None:
    created = await _create(
        api, admin_account, ttl_days=1, max_uses=3, email_domain="@ACME.ru "
    )
    invite = created["invite"]
    assert isinstance(invite, dict)
    assert (invite["max_uses"], invite["email_domain"]) == (3, "acme.ru")


@pytest.mark.parametrize(
    "body",
    [
        {"ttl_days": 0},
        {"ttl_days": 31},
        {"max_uses": 0},
        {"max_uses": 1001},
        {"email_domain": "not a domain"},
        {"role": "admin"},
    ],
)
async def test_bad_link_settings_are_rejected(
    api: httpx.AsyncClient, admin_account: User, body: dict[str, object]
) -> None:
    response = await api.post(
        "/api/v1/invites", json=body, headers=bearer(admin_account)
    )
    assert response.status_code == 422


async def test_employee_and_anonymous_cannot_manage_links(
    api: httpx.AsyncClient, account: User
) -> None:
    for method, url in (
        ("POST", "/api/v1/invites"),
        ("GET", "/api/v1/invites"),
        ("DELETE", f"/api/v1/invites/{uuid4()}"),
    ):
        as_employee = await api.request(method, url, json={}, headers=bearer(account))
        assert as_employee.status_code == 403
        anonymous = await api.request(method, url, json={})
        assert anonymous.status_code == 401


async def test_admin_cannot_revoke_other_company_link(
    api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    other = Tenant(id=uuid4(), company_code="other", name="Other")
    session.add(other)
    await session.commit()
    with tenant_scope(other.id):
        invite = Invite(
            tenant_id=other.id,
            token_hash=hash_refresh_token("x" * 43),
            expires_at=datetime.now(UTC) + timedelta(days=1),
            max_uses=5,
        )
        session.add(invite)
        await session.commit()

    response = await api.delete(
        f"/api/v1/invites/{invite.id}", headers=bearer(admin_account)
    )

    assert response.status_code == 404
    listed = await api.get("/api/v1/invites", headers=bearer(admin_account))
    assert listed.json() == []


# --- человек со ссылкой ----------------------------------------------------------


async def test_preview_names_the_company(
    api: httpx.AsyncClient, admin_account: User
) -> None:
    created = await _create(api, admin_account, email_domain="test.com")

    response = await api.post(
        "/api/v1/invites/preview",
        json={"company_code": "TEST", "token": created["token"]},
    )

    assert response.status_code == 200
    assert response.json()["company_name"] == "Test Co"
    assert response.json()["email_domain"] == "test.com"


async def test_join_creates_employee_and_signs_in(
    api: httpx.AsyncClient,
    admin_account: User,
    tenant_ctx: Tenant,
    session: AsyncSession,
) -> None:
    created = await _create(api, admin_account)

    response = await _accept(api, created["token"], "New@Test.com")

    assert response.status_code == 201, response.text
    tokens = response.json()
    me = await api.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert me.status_code == 200
    profile = me.json()
    assert profile["email"] == "new@test.com"
    assert profile["role"] == "employee"
    assert profile["company_code"] == "test"
    assert profile["must_change_password"] is False

    with tenant_scope(tenant_ctx.id):
        user = (
            await session.scalars(select(User).where(User.email == "new@test.com"))
        ).one()
    assert user.full_name == "Новый Сотрудник"
    assert user.role is UserRole.EMPLOYEE
    assert (await _invite(session, tenant_ctx)).uses == 1
    actions = set(
        (
            await session.scalars(
                select(AuditEvent.action).where(AuditEvent.actor_user_id == user.id)
            )
        ).all()
    )
    assert actions == {"user.joined_by_invite", "auth.login.succeeded"}

    # Войти потом можно обычным способом, со своим паролем.
    login = await api.post(
        "/api/v1/auth/login",
        json={
            "company_code": "test",
            "email": "new@test.com",
            "password": NEW_PASSWORD,
        },
    )
    assert login.status_code == 200


async def test_link_stops_after_max_uses(
    api: httpx.AsyncClient, admin_account: User
) -> None:
    created = await _create(api, admin_account, max_uses=1)
    assert (await _accept(api, created["token"], "one@test.com")).status_code == 201

    second = await _accept(api, created["token"], "two@test.com")

    assert second.status_code == 404
    assert "недействительна" in second.json()["detail"]


async def test_revoked_and_expired_links_do_not_work(
    api: httpx.AsyncClient,
    admin_account: User,
    tenant_ctx: Tenant,
    session: AsyncSession,
) -> None:
    revoked = await _create(api, admin_account)
    invite = revoked["invite"]
    assert isinstance(invite, dict)
    delete = await api.delete(
        f"/api/v1/invites/{invite['id']}", headers=bearer(admin_account)
    )
    assert delete.status_code == 204
    assert (await _accept(api, revoked["token"], "a@test.com")).status_code == 404

    expired = await _create(api, admin_account)
    with tenant_scope(tenant_ctx.id):
        row = (
            await session.scalars(
                select(Invite).where(
                    Invite.token_hash == hash_refresh_token(str(expired["token"]))
                )
            )
        ).one()
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()
    preview = await api.post(
        "/api/v1/invites/preview",
        json={"company_code": "test", "token": expired["token"]},
    )
    assert preview.status_code == 404
    assert (await _accept(api, expired["token"], "b@test.com")).status_code == 404

    statuses = {
        item["status"]
        for item in (
            await api.get("/api/v1/invites", headers=bearer(admin_account))
        ).json()
    }
    assert statuses == {"revoked", "expired"}


async def test_link_is_bound_to_its_company(
    api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    """Токен ищется в контексте компании из адреса: с чужим кодом — нет."""
    session.add(Tenant(id=uuid4(), company_code="other", name="Other"))
    await session.commit()
    created = await _create(api, admin_account)

    response = await _accept(api, created["token"], company="other")

    assert response.status_code == 404


async def test_unknown_token_and_inactive_company(
    api: httpx.AsyncClient,
    admin_account: User,
    tenant_ctx: Tenant,
    session: AsyncSession,
) -> None:
    assert (await _accept(api, "y" * 43)).status_code == 404

    created = await _create(api, admin_account)
    tenant_ctx.is_active = False
    await session.commit()
    assert (await _accept(api, created["token"])).status_code == 404


async def test_email_domain_restriction(
    api: httpx.AsyncClient, admin_account: User
) -> None:
    created = await _create(api, admin_account, email_domain="acme.ru")

    wrong = await _accept(api, created["token"], "anna@gmail.com")
    assert wrong.status_code == 422
    assert "@acme.ru" in wrong.json()["detail"]
    lookalike = await _accept(api, created["token"], "anna@evilacme.ru")
    assert lookalike.status_code == 422

    assert (await _accept(api, created["token"], "anna@msk.acme.ru")).status_code == 201


async def test_existing_email_must_sign_in_instead(
    api: httpx.AsyncClient, admin_account: User, account: User
) -> None:
    created = await _create(api, admin_account)

    response = await _accept(api, created["token"], account.email.upper())

    assert response.status_code == 409
    assert "войдите" in response.json()["detail"]


async def test_join_password_follows_policy(
    api: httpx.AsyncClient,
    admin_account: User,
    tenant_ctx: Tenant,
    session: AsyncSession,
) -> None:
    created = await _create(api, admin_account)

    short = await _accept(api, created["token"], password="short")
    with_email = await _accept(
        api, created["token"], "maria@test.com", password="maria-2026-pass"
    )

    assert short.status_code == 422
    assert with_email.status_code == 422
    assert (await _invite(session, tenant_ctx)).uses == 0


async def test_join_cannot_choose_role(
    api: httpx.AsyncClient, admin_account: User
) -> None:
    created = await _create(api, admin_account)

    response = await api.post(
        "/api/v1/invites/accept",
        json={
            "company_code": "test",
            "token": created["token"],
            "email": "x@test.com",
            "password": NEW_PASSWORD,
            "role": "admin",
        },
    )

    assert response.status_code == 422


async def test_public_endpoints_are_rate_limited(api: httpx.AsyncClient) -> None:
    body = {"company_code": "test", "token": "z" * 43}
    for _ in range(60):
        assert (await api.post("/api/v1/invites/preview", json=body)).status_code == 404

    response = await api.post("/api/v1/invites/preview", json=body)

    assert response.status_code == 429
