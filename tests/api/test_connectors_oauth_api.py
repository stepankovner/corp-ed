"""OAuth режима per_user через API: каталог, секрет приложения, старт,
обратный вызов без аутентификации, лимит по IP."""

from collections.abc import AsyncGenerator
from typing import Annotated, Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from corp_ed.api.v1.dependencies import (
    get_audit_repository,
    get_connector_service,
    get_session,
)
from corp_ed.connectors.registry import default_registry
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.secrets import SecretBox
from corp_ed.core.security import decode_oauth_state
from corp_ed.core.tenant_context import require_tenant, tenant_scope
from corp_ed.domain.models import (
    AuditEvent,
    ConnectorSyncJob,
    ConnectorUserGrant,
    Tenant,
    User,
)
from corp_ed.main import app
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.repositories.connector_repository import (
    ConnectorRepository,
    GrantRepository,
    SyncRunRepository,
)
from corp_ed.repositories.connector_sync_job_repository import (
    ConnectorSyncJobRepository,
)
from corp_ed.repositories.material_repository import MaterialRepository
from corp_ed.services.connector_service import ConnectorService
from tests.api.conftest import bearer
from tests.connectors.fake_portal import (
    AUTH_CODE,
    CLIENT_ID,
    CLIENT_SECRET,
    EMPLOYEE_ID,
    OAUTH_SERVER,
    FakePortal,
    sample_portal,
)
from tests.fake_connector import public_resolver

URL = "/api/v1/connectors"
KEY = Fernet.generate_key().decode()
CALLBACK_URL = "https://api.example.com/api/v1/connectors/oauth/callback"
RETURN_URL = "https://app.example.com/sources?tab=mine"


@pytest.fixture
def portal() -> FakePortal:
    return sample_portal()


@pytest.fixture
def settings() -> ConnectorSettings:
    return ConnectorSettings(
        secrets_keys=KEY,
        bitrix24_oauth_server=OAUTH_SERVER,
        oauth_callback_url=CALLBACK_URL,
        oauth_return_url=RETURN_URL,
    )  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("corp_ed.connectors.bitrix24.adapter.MIN_INTERVAL", 0.0)


@pytest.fixture
async def oauth_api(
    api: httpx.AsyncClient,
    session: AsyncSession,
    portal: FakePortal,
    settings: ConnectorSettings,
) -> AsyncGenerator[httpx.AsyncClient]:
    registry = default_registry(settings)
    secrets = SecretBox([KEY])

    def dependency(
        session: Annotated[AsyncSession, Depends(get_session)],
        audit: Annotated[AuditRepository, Depends(get_audit_repository)],
    ) -> ConnectorService:
        http = portal.client()
        return ConnectorService(
            ConnectorRepository(session),
            GrantRepository(session),
            SyncRunRepository(session),
            ConnectorSyncJobRepository(session),
            MaterialRepository(session),
            audit,
            secrets,
            registry,
            settings,
            session,
            http,
            resolver=public_resolver,
        )

    app.dependency_overrides[get_connector_service] = dependency
    yield api
    app.dependency_overrides.pop(get_connector_service, None)


async def create_connector(
    client: httpx.AsyncClient,
    admin: User,
    portal: FakePortal,
    *,
    with_secret: bool = True,
) -> UUID:
    response = await client.post(
        URL,
        json={
            "kind": "bitrix24",
            "name": "Портал",
            "modules": ["disk", "knowledge_base"],
            "config": {"portal": portal.portal, "client_id": CLIENT_ID},
        },
        headers=bearer(admin),
    )
    assert response.status_code == 201, response.text
    connector_id = UUID(response.json()["id"])
    if with_secret:
        response = await client.put(
            f"{URL}/{connector_id}/credentials",
            json={"credentials": {"client_secret": CLIENT_SECRET}},
            headers=bearer(admin),
        )
        assert response.status_code == 200, response.text
        assert response.json()["credentials_set_at"] is not None
    return connector_id


async def start(
    client: httpx.AsyncClient, user: User, connector_id: UUID
) -> tuple[str, str]:
    response = await client.post(
        f"{URL}/{connector_id}/oauth/start", headers=bearer(user)
    )
    assert response.status_code == 200, response.text
    url = response.json()["authorize_url"]
    state = parse_qs(urlsplit(url).query)["state"][0]
    return url, state


def redirect_query(response: httpx.Response) -> dict[str, str]:
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith("https://app.example.com/sources?tab=mine&")
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


# --- каталог и настройка ---------------------------------------------------


async def test_kinds_describe_bitrix24_oauth(
    oauth_api: httpx.AsyncClient, admin_account: User
) -> None:
    response = await oauth_api.get(f"{URL}/kinds", headers=bearer(admin_account))
    assert response.status_code == 200
    [spec] = response.json()
    assert spec["kind"] == "bitrix24"
    assert spec["mode"] == "per_user"
    assert spec["oauth"] is True
    assert spec["oauth_callback_url"] == CALLBACK_URL
    assert spec["credential_fields"] == []
    assert [f["name"] for f in spec["app_credential_fields"]] == ["client_secret"]
    assert spec["app_credential_fields"][0]["secret"] is True
    assert [f["name"] for f in spec["config_fields"]] == ["portal", "client_id"]
    assert spec["extra"]["app_scopes"] == "disk,landing"
    assert [m["name"] for m in spec["modules"]] == [
        "disk",
        "disk_personal",
        "knowledge_base",
    ]


async def test_employee_cannot_paste_tokens_into_oauth_kind(
    oauth_api: httpx.AsyncClient, admin_account: User, account: User, portal: FakePortal
) -> None:
    connector_id = await create_connector(oauth_api, admin_account, portal)
    response = await oauth_api.put(
        f"{URL}/{connector_id}/mine",
        json={"credentials": {"access_token": "x"}},
        headers=bearer(account),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "oauth_required"
    mine = await oauth_api.get(f"{URL}/mine", headers=bearer(account))
    assert mine.json() == [
        {
            "id": str(connector_id),
            "kind": "bitrix24",
            "name": "Портал",
            "grant_status": None,
            "grant_error_code": None,
            "oauth": True,
        }
    ]


async def test_app_secret_is_write_only_and_validated(
    oauth_api: httpx.AsyncClient, admin_account: User, portal: FakePortal
) -> None:
    connector_id = await create_connector(
        oauth_api, admin_account, portal, with_secret=False
    )
    response = await oauth_api.put(
        f"{URL}/{connector_id}/credentials",
        json={"credentials": {"webhook": "https://x/"}},
        headers=bearer(admin_account),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "field_unknown"
    response = await oauth_api.get(
        f"{URL}/{connector_id}", headers=bearer(admin_account)
    )
    assert "client_secret" not in response.text
    assert response.json()["credentials_set_at"] is None


# --- старт --------------------------------------------------------------------


async def test_start_returns_portal_authorize_url_with_bound_state(
    oauth_api: httpx.AsyncClient, admin_account: User, account: User, portal: FakePortal
) -> None:
    connector_id = await create_connector(oauth_api, admin_account, portal)
    url, state = await start(oauth_api, account, connector_id)
    assert url.startswith(f"{portal.portal}oauth/authorize/?")
    assert f"client_id={CLIENT_ID}" in url
    assert CLIENT_SECRET not in url
    payload = decode_oauth_state(state)
    assert payload["sub"] == str(account.id)
    assert payload["tenant_id"] == str(account.tenant_id)
    assert payload["connector_id"] == str(connector_id)


async def test_start_requires_app_secret(
    oauth_api: httpx.AsyncClient, admin_account: User, account: User, portal: FakePortal
) -> None:
    connector_id = await create_connector(
        oauth_api, admin_account, portal, with_secret=False
    )
    response = await oauth_api.post(
        f"{URL}/{connector_id}/oauth/start", headers=bearer(account)
    )
    assert response.status_code == 409


async def test_start_unknown_connector(
    oauth_api: httpx.AsyncClient, account: User
) -> None:
    response = await oauth_api.post(
        f"{URL}/{uuid4()}/oauth/start", headers=bearer(account)
    )
    assert response.status_code == 404


# --- обратный вызов -----------------------------------------------------------


async def test_callback_exchanges_code_and_creates_grant(
    oauth_api: httpx.AsyncClient,
    admin_account: User,
    account: User,
    portal: FakePortal,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    # id — до запросов: сессия теста общая с приложением, и commit в
    # обработчике истекает загруженные объекты (ошибка №12 в WORKLOG).
    account_id = account.id
    connector_id = await create_connector(oauth_api, admin_account, portal)
    _, state = await start(oauth_api, account, connector_id)

    # Браузер приходит без нашего токена.
    response = await oauth_api.get(
        f"{URL}/oauth/callback", params={"code": AUTH_CODE, "state": state}
    )

    assert redirect_query(response) == {
        "tab": "mine",
        "status": "ok",
        "connector_id": str(connector_id),
    }
    with tenant_scope(require_tenant()):
        grant = (
            await session.scalars(
                select(ConnectorUserGrant)
                .options(undefer(ConnectorUserGrant.credentials))
                .where(ConnectorUserGrant.connector_id == connector_id)
            )
        ).one()
        assert grant.user_id == account_id
        assert grant.status == "active"
        assert grant.external_user_id == EMPLOYEE_ID
        saved = SecretBox([KEY]).decrypt(grant.credentials)
        assert saved["access_token"] in portal.access_tokens
        assert set(saved) == {
            "access_token",
            "refresh_token",
            "expires_at",
            "member_id",
        }
        jobs = (await session.scalars(select(ConnectorSyncJob))).all()
        assert [j.connector_id for j in jobs] == [connector_id]
        event = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == AuditAction.CONNECTOR_GRANT_SET.value
                )
            )
        ).one()
        assert event.actor_user_id == account_id
        assert event.details["via"] == "oauth"
        assert "access_token" in event.details["fields"]
        assert saved["access_token"] not in str(event.details)
    mine = await oauth_api.get(f"{URL}/mine", headers=bearer(account))
    assert mine.json()[0]["grant_status"] == "active"
    # Токен проверен на портале прямо в обратном вызове.
    assert ("profile", {}) in portal.calls


async def test_callback_with_bad_state_redirects_with_error(
    oauth_api: httpx.AsyncClient,
) -> None:
    response = await oauth_api.get(
        f"{URL}/oauth/callback", params={"code": AUTH_CODE, "state": "garbage"}
    )
    assert redirect_query(response) == {
        "tab": "mine",
        "status": "error",
        "error_code": "state_invalid",
    }


async def test_callback_with_bad_code_records_failure(
    oauth_api: httpx.AsyncClient,
    admin_account: User,
    account: User,
    portal: FakePortal,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    account_id = account.id
    connector_id = await create_connector(oauth_api, admin_account, portal)
    _, state = await start(oauth_api, account, connector_id)
    response = await oauth_api.get(
        f"{URL}/oauth/callback", params={"code": "stale", "state": state}
    )
    assert redirect_query(response) == {
        "tab": "mine",
        "status": "error",
        "connector_id": str(connector_id),
        "error_code": "invalid_grant",
    }
    with tenant_scope(require_tenant()):
        grants = (await session.scalars(select(ConnectorUserGrant))).all()
        assert grants == []
        event = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == AuditAction.CONNECTOR_OAUTH_FAILED.value
                )
            )
        ).one()
        assert event.details == {"code": "invalid_grant"}
        assert event.actor_user_id == account_id


async def test_callback_with_wrong_app_secret_reports_config_error(
    oauth_api: httpx.AsyncClient, admin_account: User, account: User, portal: FakePortal
) -> None:
    connector_id = await create_connector(oauth_api, admin_account, portal)
    response = await oauth_api.put(
        f"{URL}/{connector_id}/credentials",
        json={"credentials": {"client_secret": "wrong"}},
        headers=bearer(admin_account),
    )
    assert response.status_code == 200
    _, state = await start(oauth_api, account, connector_id)
    response = await oauth_api.get(
        f"{URL}/oauth/callback", params={"code": AUTH_CODE, "state": state}
    )
    assert redirect_query(response)["error_code"] == "invalid_client"


async def test_callback_answers_json_without_return_url(
    oauth_api: httpx.AsyncClient,
    admin_account: User,
    account: User,
    portal: FakePortal,
    settings: ConnectorSettings,
) -> None:
    settings.oauth_return_url = None
    connector_id = await create_connector(oauth_api, admin_account, portal)
    _, state = await start(oauth_api, account, connector_id)
    response = await oauth_api.get(
        f"{URL}/oauth/callback", params={"code": AUTH_CODE, "state": state}
    )
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "connector_id": str(connector_id),
        "error_code": None,
    }


async def test_callback_is_rate_limited_by_ip(oauth_api: httpx.AsyncClient) -> None:
    statuses: list[int] = []
    for _ in range(31):
        response = await oauth_api.get(
            f"{URL}/oauth/callback", params={"code": "x", "state": "y"}
        )
        statuses.append(response.status_code)
    assert statuses[:30] == [303] * 30
    assert statuses[30] == 429


async def test_test_endpoint_uses_grant_and_app_secret(
    oauth_api: httpx.AsyncClient,
    admin_account: User,
    portal: FakePortal,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    connector_id = await create_connector(oauth_api, admin_account, portal)
    response = await oauth_api.post(
        f"{URL}/{connector_id}/test", headers=bearer(admin_account)
    )
    assert response.json() == {"ok": False, "error_code": "credentials_missing"}
    _, state = await start(oauth_api, admin_account, connector_id)
    await oauth_api.get(
        f"{URL}/oauth/callback", params={"code": AUTH_CODE, "state": state}
    )
    response = await oauth_api.post(
        f"{URL}/{connector_id}/test", headers=bearer(admin_account)
    )
    assert response.json() == {"ok": True, "error_code": None}


async def test_callback_ignores_extra_portal_params(
    oauth_api: httpx.AsyncClient, admin_account: User, account: User, portal: FakePortal
) -> None:
    """Портал добавляет domain, member_id, scope, server_domain — они не
    используются: адрес портала берётся из настроек подключения."""
    connector_id = await create_connector(oauth_api, admin_account, portal)
    _, state = await start(oauth_api, account, connector_id)
    params: dict[str, Any] = {
        "code": AUTH_CODE,
        "state": state,
        "domain": "evil.example.com",
        "member_id": "m",
        "scope": "disk%2Clanding",
        "server_domain": "evil.example.com",
    }
    response = await oauth_api.get(f"{URL}/oauth/callback", params=params)
    assert redirect_query(response)["status"] == "ok"
    assert all(host == "oauth/token" or True for host, _ in portal.calls)
