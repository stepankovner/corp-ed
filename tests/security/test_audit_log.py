"""Журнал аудита: что пишется, что не пишется, и что его нельзя переписать."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import AuditEvent, Tenant, User, UserRole
from corp_ed.repositories.audit_repository import AuditAction
from tests.api.conftest import PASSWORD, bearer, login


async def _events(session: AsyncSession, action: AuditAction) -> list[AuditEvent]:
    result = await session.execute(
        select(AuditEvent)
        .where(AuditEvent.action == action.value)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars())


async def test_successful_login_is_audited_with_request_metadata(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    response = await login(api, account.email)

    [event] = await _events(session, AuditAction.LOGIN_SUCCEEDED)
    assert event.actor_user_id == account.id
    assert event.tenant_id == account.tenant_id
    assert event.request_id == response.headers["x-request-id"]
    assert event.ip is not None


async def test_failed_login_is_audited_without_password(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    await login(api, account.email, "wrong-password-123")

    [event] = await _events(session, AuditAction.LOGIN_FAILED)
    assert event.details["reason"] == "wrong_password"
    assert event.details["email"] == account.email
    assert "wrong-password-123" not in str(event.details)
    assert "password" not in event.details


async def test_failed_login_for_unknown_company_is_kept(
    api: httpx.AsyncClient, session: AsyncSession
) -> None:
    """Тенант неизвестен, но след попытки остаться обязан."""
    await api.post(
        "/api/v1/auth/login",
        json={"company_code": "nope", "email": "a@b.ru", "password": PASSWORD},
    )

    [event] = await _events(session, AuditAction.LOGIN_FAILED)
    assert event.tenant_id is None
    assert event.details["reason"] == "unknown_or_inactive_company"


async def test_admin_actions_are_audited(
    api: httpx.AsyncClient,
    admin_account: User,
    account: User,
    session: AsyncSession,
) -> None:
    headers = bearer(admin_account)
    created = await api.post(
        "/api/v1/users", json={"email": "new@test.com"}, headers=headers
    )
    await api.patch(
        f"/api/v1/users/{account.id}", json={"is_active": False}, headers=headers
    )
    await api.post(f"/api/v1/users/{account.id}/reset-password", headers=headers)

    [made] = await _events(session, AuditAction.USER_CREATED)
    [changed] = await _events(session, AuditAction.USER_UPDATED)
    [reset] = await _events(session, AuditAction.USER_PASSWORD_RESET)

    assert made.actor_user_id == admin_account.id
    assert made.target_id == created.json()["user"]["id"]
    assert changed.details["before"]["is_active"] is True
    assert changed.details["after"]["is_active"] is False
    assert reset.target_id == str(account.id)
    # Временный пароль в журнал не попадает.
    assert created.json()["temporary_password"] not in str(made.details)


async def test_refresh_reuse_is_audited(
    api: httpx.AsyncClient, account: User, session: AsyncSession
) -> None:
    stolen = (await login(api, account.email)).json()["refresh_token"]
    await api.post("/api/v1/auth/refresh", json={"refresh_token": stolen})
    await api.post("/api/v1/auth/refresh", json={"refresh_token": stolen})

    [event] = await _events(session, AuditAction.REFRESH_REUSE_DETECTED)
    assert event.actor_user_id == account.id


# --- чтение ------------------------------------------------------------------


async def test_employee_cannot_read_audit(
    api: httpx.AsyncClient, account: User
) -> None:
    response = await api.get("/api/v1/audit", headers=bearer(account))
    assert response.status_code == 403


async def test_admin_reads_only_own_company(
    api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    other = Tenant(id=uuid4(), company_code="other", name="Other")
    session.add(other)
    await session.commit()
    session.add(AuditEvent(tenant_id=other.id, action="auth.login.failed"))
    await session.commit()
    await login(api, admin_account.email)

    response = await api.get("/api/v1/audit", headers=bearer(admin_account))

    assert response.status_code == 200
    actions = [event["action"] for event in response.json()]
    assert actions == ["auth.login.succeeded"]


async def test_audit_page_size_is_bounded(
    api: httpx.AsyncClient, admin_account: User
) -> None:
    response = await api.get(
        "/api/v1/audit", params={"limit": 10_000}, headers=bearer(admin_account)
    )
    assert response.status_code == 422


# --- неизменяемость на уровне базы -------------------------------------------


async def _one_event(session: AsyncSession, tenant: Tenant) -> AuditEvent:
    event = AuditEvent(tenant_id=tenant.id, action="auth.logout")
    session.add(event)
    await session.commit()
    return event


async def test_audit_rows_cannot_be_updated(
    session: AsyncSession, tenant_ctx: Tenant
) -> None:
    event = await _one_event(session, tenant_ctx)

    with pytest.raises(DBAPIError, match="append-only"):
        await session.execute(
            update(AuditEvent)
            .where(AuditEvent.id == event.id)
            .values(action="auth.login.succeeded")
        )
    await session.rollback()


async def test_recent_audit_rows_cannot_be_deleted(
    session: AsyncSession, tenant_ctx: Tenant
) -> None:
    event = await _one_event(session, tenant_ctx)

    with pytest.raises(DBAPIError, match="retention"):
        await session.execute(delete(AuditEvent).where(AuditEvent.id == event.id))
    await session.rollback()


async def test_rows_older_than_retention_can_be_purged(
    session: AsyncSession, tenant_ctx: Tenant
) -> None:
    old = AuditEvent(
        tenant_id=tenant_ctx.id,
        action="auth.logout",
        created_at=datetime.now(UTC) - timedelta(days=400),
    )
    session.add(old)
    await session.commit()

    await session.execute(delete(AuditEvent).where(AuditEvent.id == old.id))
    await session.commit()


async def test_deleting_user_only_nulls_actor(
    session: AsyncSession, tenant_ctx: Tenant
) -> None:
    """ON DELETE SET NULL — единственное разрешённое изменение записи."""
    with tenant_scope(tenant_ctx.id):
        user = User(
            tenant_id=tenant_ctx.id,
            email="gone@test.com",
            role=UserRole.EMPLOYEE,
            hashed_password="x",
        )
        session.add(user)
        await session.commit()
        session.add(
            AuditEvent(tenant_id=tenant_ctx.id, actor_user_id=user.id, action="x")
        )
        await session.commit()

        await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
        await session.commit()

    event = (
        await session.execute(
            select(AuditEvent).execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert event.actor_user_id is None
    assert event.action == "x"
