"""Люди компании — для администратора (ТЗ §2, §7).

Главное здесь — авторизация: EMPLOYEE не управляет людьми, ADMIN не
дотягивается до чужой компании (BOLA, OWASP API1:2023), не может оставить
компанию без администратора и трогает только членство — не учётку
человека и не его вход в другие компании.
"""

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import Account, MemberStatus, Tenant, User, UserRole
from tests.api.conftest import bearer, login, refresh_token_of, refresh_with
from tests.factories import make_user


async def _other_company(session: AsyncSession, code: str = "other") -> Tenant:
    other = Tenant(id=uuid4(), company_code=code, name="Other")
    session.add(other)
    await session.commit()
    return other


async def test_employee_cannot_manage_users(
    api: httpx.AsyncClient, account: User
) -> None:
    headers = bearer(account)
    target = f"/api/v1/users/{account.id}"

    assert (await api.get("/api/v1/users", headers=headers)).status_code == 403
    patched = await api.patch(target, json={"role": "admin"}, headers=headers)
    assert patched.status_code == 403
    assert (await api.delete(target, headers=headers)).status_code == 403
    assert (await api.post(f"{target}/approve", headers=headers)).status_code == 403


async def test_anonymous_cannot_manage_users(api: httpx.AsyncClient) -> None:
    assert (await api.get("/api/v1/users")).status_code == 401


async def test_admin_no_longer_creates_accounts_or_passwords(
    api: httpx.AsyncClient, admin_account: User, account: User
) -> None:
    """Учётки заводят люди сами, пароль восстанавливают по почте (03.10)."""
    headers = bearer(admin_account)
    created = await api.post(
        "/api/v1/users", json={"email": "x@test.com"}, headers=headers
    )
    reset = await api.post(
        f"/api/v1/users/{account.id}/reset-password", headers=headers
    )
    assert created.status_code == 405
    assert reset.status_code == 404


async def test_admin_lists_own_company_with_statuses(
    api: httpx.AsyncClient, admin_account: User, account: User, session: AsyncSession
) -> None:
    other = await _other_company(session)
    with tenant_scope(other.id):
        session.add(make_user(email="stranger@other.com", role=UserRole.ADMIN))
        await session.commit()
    pending = make_user(email="new@test.com", status=MemberStatus.PENDING)
    gone = make_user(email="gone@test.com", status=MemberStatus.LEFT)
    session.add_all([pending, gone])
    await session.commit()

    response = await api.get("/api/v1/users", headers=bearer(admin_account))

    assert response.status_code == 200
    statuses = {user["email"]: user["status"] for user in response.json()}
    assert statuses == {
        admin_account.email: "active",
        account.email: "active",
        "new@test.com": "pending",
    }
    assert all("hashed_password" not in user for user in response.json())
    assert all("token_version" not in user for user in response.json())


async def test_admin_cannot_touch_other_company_user(
    api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    """Чужой id — 404, как несуществующий: наличие не подтверждается."""
    other = await _other_company(session)
    with tenant_scope(other.id):
        stranger = make_user(email="stranger@other.com")
        session.add(stranger)
        await session.commit()

    headers = bearer(admin_account)
    target = f"/api/v1/users/{stranger.id}"
    patched = await api.patch(target, json={"blocked": True}, headers=headers)
    removed = await api.delete(target, headers=headers)

    assert patched.status_code == 404
    assert removed.status_code == 404
    with tenant_scope(other.id):
        await session.refresh(stranger)
    assert stranger.status is MemberStatus.ACTIVE


async def test_block_drops_the_company_but_keeps_the_account(
    api: httpx.AsyncClient, admin_account: User, account: User
) -> None:
    signed_in = await login(api, account.email)
    tokens = signed_in.json()

    response = await api.patch(
        f"/api/v1/users/{account.id}",
        json={"blocked": True},
        headers=bearer(admin_account),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "blocked"

    old = {"Authorization": f"Bearer {tokens['access_token']}"}
    assert (await api.get("/api/v1/auth/me", headers=old)).status_code == 401
    refreshed = await refresh_with(api, refresh_token_of(signed_in))
    assert refreshed.status_code == 200
    new = {"Authorization": f"Bearer {refreshed.json()['access_token']}"}
    assert (await api.get("/api/v1/auth/me", headers=new)).json()["company"] is None


async def test_block_in_one_company_keeps_the_other(
    api: httpx.AsyncClient, admin_account: User, account: User, session: AsyncSession
) -> None:
    other = await _other_company(session)
    with tenant_scope(other.id):
        elsewhere = make_user(email="x@x.ru", account=account.account)
        session.add(elsewhere)
        await session.commit()
    elsewhere_headers = bearer(elsewhere)

    await api.patch(
        f"/api/v1/users/{account.id}",
        json={"blocked": True},
        headers=bearer(admin_account),
    )

    me = await api.get("/api/v1/auth/me", headers=elsewhere_headers)
    assert me.status_code == 200
    assert me.json()["company"]["tenant_id"] == str(other.id)


async def test_role_change_kills_existing_sessions(
    api: httpx.AsyncClient, admin_account: User, account: User
) -> None:
    old = bearer(account)

    await api.patch(
        f"/api/v1/users/{account.id}",
        json={"role": "admin"},
        headers=bearer(admin_account),
    )

    assert (await api.get("/api/v1/auth/me", headers=old)).status_code == 401


async def test_admin_cannot_demote_block_or_remove_self(
    api: httpx.AsyncClient, admin_account: User
) -> None:
    headers = bearer(admin_account)
    target = f"/api/v1/users/{admin_account.id}"

    demote = await api.patch(target, json={"role": "employee"}, headers=headers)
    block = await api.patch(target, json={"blocked": True}, headers=headers)
    remove = await api.delete(target, headers=headers)

    assert demote.status_code == 409
    assert block.status_code == 409
    assert remove.status_code == 409


async def test_update_rejects_unknown_fields(
    api: httpx.AsyncClient, admin_account: User, account: User
) -> None:
    """Почту и имя меняет сам человек в учётке, не админ (mass assignment)."""
    response = await api.patch(
        f"/api/v1/users/{account.id}",
        json={"email": "evil@test.com"},
        headers=bearer(admin_account),
    )
    assert response.status_code == 422


async def test_remove_keeps_account_and_frees_the_seat(
    api: httpx.AsyncClient,
    admin_account: User,
    account: User,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    old = bearer(account)
    response = await api.delete(
        f"/api/v1/users/{account.id}", headers=bearer(admin_account)
    )
    assert response.status_code == 204

    assert (await api.get("/api/v1/auth/me", headers=old)).status_code == 401
    listed = await api.get("/api/v1/users", headers=bearer(admin_account))
    assert account.email not in {user["email"] for user in listed.json()}
    await session.refresh(account)
    assert account.status is MemberStatus.LEFT
    assert account.left_at is not None
    still_there = await session.scalar(
        select(Account).where(Account.email == account.email)
    )
    assert still_there is not None
    # Повторное удаление — 404: ушедшего в компании больше нет.
    again = await api.delete(
        f"/api/v1/users/{account.id}", headers=bearer(admin_account)
    )
    assert again.status_code == 404


async def test_approve_and_reject_pending(
    api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    first = make_user(email="first@test.com", status=MemberStatus.PENDING)
    second = make_user(email="second@test.com", status=MemberStatus.PENDING)
    session.add_all([first, second])
    await session.commit()
    headers = bearer(admin_account)

    approved = await api.post(f"/api/v1/users/{first.id}/approve", headers=headers)
    rejected = await api.post(f"/api/v1/users/{second.id}/reject", headers=headers)
    again = await api.post(f"/api/v1/users/{first.id}/approve", headers=headers)

    assert approved.status_code == 200
    assert approved.json()["status"] == "active"
    assert rejected.status_code == 204
    assert again.status_code == 409
    await session.refresh(second)
    assert second.status is MemberStatus.LEFT


async def test_approve_needs_a_free_seat(
    api: httpx.AsyncClient,
    admin_account: User,
    account: User,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    tenant_ctx.seats = 2  # админ и сотрудник уже заняли оба места
    waiting = make_user(email="waiting@test.com", status=MemberStatus.PENDING)
    session.add(waiting)
    await session.commit()

    response = await api.post(
        f"/api/v1/users/{waiting.id}/approve", headers=bearer(admin_account)
    )
    assert response.status_code == 409


async def test_last_admin_guard_in_service(
    session: AsyncSession, tenant_ctx: Tenant, admin_account: User
) -> None:
    """Через HTTP сюда не дойти: вызывающий — сам активный админ, а себя
    менять нельзя. Проверка — второй рубеж на случай новых путей вызова
    (CLI, фоновые задачи), поэтому тестируется на уровне сервиса."""
    from corp_ed.core.exceptions import LastAdminError
    from corp_ed.repositories.audit_repository import AuditRepository
    from corp_ed.repositories.user_repository import UserRepository
    from corp_ed.services.user_service import UserService

    service = UserService(UserRepository(session), AuditRepository(session), session)
    actor = make_user(
        id=uuid4(),
        tenant_id=tenant_ctx.id,
        email="ghost@test.com",
        role=UserRole.ADMIN,
        is_active=False,
    )

    with pytest.raises(LastAdminError):
        await service.update_user(actor, admin_account.id, role=UserRole.EMPLOYEE)
    with pytest.raises(LastAdminError):
        await service.remove(actor, admin_account.id)
