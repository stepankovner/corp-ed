"""Управление пользователями компании (ADMIN).

Главное здесь — авторизация: EMPLOYEE не управляет пользователями,
ADMIN не дотягивается до чужой компании (BOLA, OWASP API1:2023) и не
может оставить компанию без администратора.
"""

from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import Tenant, User, UserRole
from tests.api.conftest import PASSWORD, bearer, login


async def test_employee_cannot_manage_users(
    api: httpx.AsyncClient, account: User
) -> None:
    headers = bearer(account)
    target = f"/api/v1/users/{account.id}"

    assert (await api.get("/api/v1/users", headers=headers)).status_code == 403
    created = await api.post(
        "/api/v1/users", json={"email": "x@test.com"}, headers=headers
    )
    assert created.status_code == 403
    patched = await api.patch(target, json={"role": "admin"}, headers=headers)
    assert patched.status_code == 403
    reset = await api.post(f"{target}/reset-password", headers=headers)
    assert reset.status_code == 403


async def test_anonymous_cannot_manage_users(api: httpx.AsyncClient) -> None:
    assert (await api.get("/api/v1/users")).status_code == 401


async def test_admin_lists_only_own_company(
    api: httpx.AsyncClient, admin_account: User, account: User, session: AsyncSession
) -> None:
    other = Tenant(id=uuid4(), company_code="other", name="Other")
    session.add(other)
    await session.commit()
    with tenant_scope(other.id):
        session.add(
            User(
                tenant_id=other.id,
                email="stranger@other.com",
                role=UserRole.ADMIN,
                hashed_password="x",
            )
        )
        await session.commit()

    response = await api.get("/api/v1/users", headers=bearer(admin_account))

    assert response.status_code == 200
    emails = {user["email"] for user in response.json()}
    assert emails == {admin_account.email, account.email}
    assert all("hashed_password" not in user for user in response.json())
    assert all("token_version" not in user for user in response.json())


async def test_admin_creates_user_with_temporary_password(
    api: httpx.AsyncClient, admin_account: User
) -> None:
    response = await api.post(
        "/api/v1/users",
        json={"email": "New.Person@Test.com", "full_name": "Новый Сотрудник"},
        headers=bearer(admin_account),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["user"]["email"] == "new.person@test.com"
    assert body["user"]["role"] == "employee"
    assert body["user"]["must_change_password"] is True
    temporary = body["temporary_password"]
    assert len(temporary) >= 16

    logged_in = await login(api, "new.person@test.com", temporary)
    assert logged_in.status_code == 200


async def test_admin_set_password_must_pass_policy(
    api: httpx.AsyncClient, admin_account: User
) -> None:
    response = await api.post(
        "/api/v1/users",
        json={"email": "p@test.com", "password": "password1234"},
        headers=bearer(admin_account),
    )
    assert response.status_code == 422


async def test_duplicate_email_is_conflict(
    api: httpx.AsyncClient, admin_account: User, account: User
) -> None:
    response = await api.post(
        "/api/v1/users",
        json={"email": account.email.upper()},
        headers=bearer(admin_account),
    )
    assert response.status_code == 409


@pytest.mark.parametrize(
    "extra",
    [
        {"tenant_id": "00000000-0000-0000-0000-000000000000"},
        {"is_active": False},
        {"hashed_password": "$argon2id$..."},
        {"token_version": 100},
    ],
)
async def test_create_user_rejects_mass_assignment(
    api: httpx.AsyncClient, admin_account: User, extra: dict[str, object]
) -> None:
    response = await api.post(
        "/api/v1/users",
        json={"email": "m@test.com", **extra},
        headers=bearer(admin_account),
    )
    assert response.status_code == 422


async def test_admin_cannot_touch_other_company_user(
    api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    """Чужой id — 404, как несуществующий: наличие не подтверждается."""
    other = Tenant(id=uuid4(), company_code="other", name="Other")
    session.add(other)
    await session.commit()
    with tenant_scope(other.id):
        stranger = User(
            tenant_id=other.id,
            email="stranger@other.com",
            role=UserRole.EMPLOYEE,
            hashed_password="x",
        )
        session.add(stranger)
        await session.commit()

    headers = bearer(admin_account)
    patched = await api.patch(
        f"/api/v1/users/{stranger.id}", json={"is_active": False}, headers=headers
    )
    reset = await api.post(
        f"/api/v1/users/{stranger.id}/reset-password", headers=headers
    )

    assert patched.status_code == 404
    assert reset.status_code == 404
    await session.refresh(stranger)
    assert stranger.is_active is True


async def test_deactivation_kills_existing_sessions(
    api: httpx.AsyncClient, admin_account: User, account: User
) -> None:
    tokens = (await login(api, account.email)).json()

    response = await api.patch(
        f"/api/v1/users/{account.id}",
        json={"is_active": False},
        headers=bearer(admin_account),
    )
    assert response.status_code == 200

    me = await api.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert me.status_code == 401
    refreshed = await api.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert refreshed.status_code == 401


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


async def test_admin_cannot_demote_or_block_self(
    api: httpx.AsyncClient, admin_account: User
) -> None:
    headers = bearer(admin_account)
    target = f"/api/v1/users/{admin_account.id}"

    demote = await api.patch(target, json={"role": "employee"}, headers=headers)
    block = await api.patch(target, json={"is_active": False}, headers=headers)

    assert demote.status_code == 409
    assert block.status_code == 409


async def test_admin_can_rename_self(
    api: httpx.AsyncClient, admin_account: User
) -> None:
    response = await api.patch(
        f"/api/v1/users/{admin_account.id}",
        json={"full_name": "Главный"},
        headers=bearer(admin_account),
    )
    assert response.status_code == 200
    assert response.json()["full_name"] == "Главный"


async def test_last_admin_guard_in_service(
    session: AsyncSession, tenant_ctx: Tenant, admin_account: User
) -> None:
    """Через HTTP сюда не дойти: вызывающий — сам активный админ, а себя
    менять нельзя. Проверка — второй рубеж на случай новых путей вызова
    (CLI, фоновые задачи), поэтому тестируется на уровне сервиса."""
    from corp_ed.core.exceptions import LastAdminError
    from corp_ed.repositories.audit_repository import AuditRepository
    from corp_ed.repositories.refresh_token_repository import RefreshTokenRepository
    from corp_ed.repositories.user_repository import UserRepository
    from corp_ed.services.user_service import UserService

    service = UserService(
        UserRepository(session),
        RefreshTokenRepository(session),
        AuditRepository(session),
        session,
    )
    actor = User(
        id=uuid4(),
        tenant_id=tenant_ctx.id,
        email="ghost@test.com",
        role=UserRole.ADMIN,
        hashed_password="x",
        is_active=False,
    )

    with pytest.raises(LastAdminError):
        await service.update_user(actor, admin_account.id, role=UserRole.EMPLOYEE)


async def test_reset_password_issues_new_temporary_and_logs_out(
    api: httpx.AsyncClient, admin_account: User, account: User
) -> None:
    tokens = (await login(api, account.email)).json()

    response = await api.post(
        f"/api/v1/users/{account.id}/reset-password", headers=bearer(admin_account)
    )

    assert response.status_code == 200
    temporary = response.json()["temporary_password"]
    assert (await login(api, account.email)).status_code == 401
    assert (await login(api, account.email, temporary)).status_code == 200
    me = await api.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert me.status_code == 401
    assert temporary != PASSWORD
