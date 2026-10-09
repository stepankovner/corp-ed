"""WebDAV-диски через API: виды в каталоге только после включения,
форма (адрес, папки, логин), учётка сотрудника, проверка, OAuth2
Nextcloud."""

from collections.abc import AsyncGenerator
from typing import Annotated
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

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
from corp_ed.core.tenant_context import require_tenant, tenant_scope
from corp_ed.domain.models import ConnectorUserGrant, Tenant, User
from corp_ed.main import app
from corp_ed.repositories.audit_repository import AuditRepository
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
from tests.connectors.fake_webdav import (
    AUTH_CODE,
    CLIENT_ID,
    CLIENT_SECRET,
    IVAN,
    IVAN_LOGIN,
    IVAN_PASSWORD,
    SERVER,
    FakeDav,
    sample_nextcloud,
)
from tests.fake_connector import public_resolver

URL = "/api/v1/connectors"
KEY = Fernet.generate_key().decode()
CALLBACK_URL = "https://api.example.com/api/v1/connectors/oauth/callback"
RETURN_URL = "https://app.example.com/sources?tab=mine"
WEBDAV_KINDS = (
    "nextcloud,nextcloud_oauth,owncloud,seafile,vk_workspace_disk,mailru_cloud,webdav"
)


@pytest.fixture
def server() -> FakeDav:
    return sample_nextcloud()


@pytest.fixture
def preview_kinds() -> str:
    return WEBDAV_KINDS


@pytest.fixture
async def webdav_api(
    api: httpx.AsyncClient,
    session: AsyncSession,
    server: FakeDav,
    preview_kinds: str,
) -> AsyncGenerator[httpx.AsyncClient]:
    settings = ConnectorSettings(
        secrets_keys=KEY,
        preview_kinds=preview_kinds,
        oauth_callback_url=CALLBACK_URL,
        oauth_return_url=RETURN_URL,
    )  # type: ignore[arg-type]
    registry = default_registry(settings)
    secrets = SecretBox([KEY])

    def dependency(
        session: Annotated[AsyncSession, Depends(get_session)],
        audit: Annotated[AuditRepository, Depends(get_audit_repository)],
    ) -> ConnectorService:
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
            server.client(),
            resolver=public_resolver,
            limiter=app.state.rate_limiter,
        )

    app.dependency_overrides[get_connector_service] = dependency
    yield api
    app.dependency_overrides.pop(get_connector_service, None)


async def create(
    client: httpx.AsyncClient, admin: User, kind: str, config: dict[str, str]
) -> httpx.Response:
    return await client.post(
        URL,
        json={"kind": kind, "name": "Файлы", "modules": ["files"], "config": config},
        headers=bearer(admin),
    )


@pytest.mark.parametrize("preview_kinds", [""])
async def test_webdav_kinds_wait_for_live_check(
    webdav_api: httpx.AsyncClient, admin_account: User
) -> None:
    response = await webdav_api.get(f"{URL}/kinds", headers=bearer(admin_account))
    assert "vk_workspace_disk" not in {k["kind"] for k in response.json()}
    response = await create(
        webdav_api, admin_account, "vk_workspace_disk", {"server": SERVER}
    )
    assert response.status_code == 422
    assert response.json()["code"] == "kind_unknown"


async def test_kinds_describe_webdav_forms(
    webdav_api: httpx.AsyncClient, admin_account: User
) -> None:
    response = await webdav_api.get(f"{URL}/kinds", headers=bearer(admin_account))
    by_kind = {k["kind"]: k for k in response.json()}
    assert set(WEBDAV_KINDS.split(",")) <= set(by_kind)
    nextcloud = by_kind["nextcloud"]
    assert nextcloud["mode"] == "per_user" and nextcloud["oauth"] is False
    assert [f["name"] for f in nextcloud["config_fields"]] == ["server", "folders"]
    assert [(f["name"], f["secret"]) for f in nextcloud["credential_fields"]] == [
        ("login", False),
        ("password", True),
    ]
    oauth = by_kind["nextcloud_oauth"]
    assert oauth["oauth"] is True
    assert oauth["oauth_callback_url"] == CALLBACK_URL
    assert [f["name"] for f in oauth["config_fields"]] == [
        "server",
        "client_id",
        "folders",
    ]
    assert [f["name"] for f in by_kind["mailru_cloud"]["config_fields"]] == ["folders"]


@pytest.mark.parametrize(
    ("config", "code"),
    [
        ({"server": "http://cloud.example.ru/"}, "scheme_not_https"),
        ({"server": "https://10.0.0.5/"}, "address_not_public"),
        ({"server": SERVER, "folders": "Документы/../Чужое"}, "folders_invalid"),
    ],
)
async def test_invalid_webdav_forms_are_rejected(
    webdav_api: httpx.AsyncClient,
    admin_account: User,
    config: dict[str, str],
    code: str,
) -> None:
    response = await create(webdav_api, admin_account, "nextcloud", config)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == code


async def test_own_app_password_is_validated_and_checked(
    webdav_api: httpx.AsyncClient,
    admin_account: User,
    server: FakeDav,
) -> None:
    """Проверка («Проверить») — у админа и по его собственной учётке:
    в режиме per_user она идёт грантом того, кто проверяет."""
    response = await create(
        webdav_api, admin_account, "nextcloud", {"server": SERVER, "folders": "Проекты"}
    )
    assert response.status_code == 201, response.text
    connector_id = response.json()["id"]
    employee = bearer(admin_account)

    response = await webdav_api.put(
        f"{URL}/{connector_id}/mine",
        json={"credentials": {"login": "a:b", "password": "x"}},
        headers=employee,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "login_invalid"

    mine = await webdav_api.get(f"{URL}/mine", headers=employee)
    [item] = mine.json()
    assert item["oauth"] is False
    assert [(f["name"], f["secret"]) for f in item["credential_fields"]] == [
        ("login", False),
        ("password", True),
    ]

    # Неверный пароль приложения — ошибка формы сразу, гранта нет.
    response = await webdav_api.put(
        f"{URL}/{connector_id}/mine",
        json={"credentials": {"login": IVAN_LOGIN, "password": "wrong"}},
        headers=employee,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "auth_failed"
    assert "wrong" not in response.text
    response = await webdav_api.post(f"{URL}/{connector_id}/test", headers=employee)
    assert response.json() == {"ok": False, "error_code": "credentials_missing"}

    response = await webdav_api.put(
        f"{URL}/{connector_id}/mine",
        json={"credentials": {"login": IVAN_LOGIN, "password": IVAN_PASSWORD}},
        headers=employee,
    )
    assert response.status_code == 204
    assert IVAN_PASSWORD not in response.text
    response = await webdav_api.post(f"{URL}/{connector_id}/test", headers=employee)
    assert response.json() == {"ok": True, "error_code": None}
    assert ("PROPFIND", f"/remote.php/dav/files/{IVAN}/", "0") in server.calls


async def test_nextcloud_oauth_start_and_callback_create_grant(
    webdav_api: httpx.AsyncClient,
    admin_account: User,
    account: User,
    server: FakeDav,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    account_id = account.id
    response = await create(
        webdav_api,
        admin_account,
        "nextcloud_oauth",
        {"server": SERVER, "client_id": CLIENT_ID},
    )
    assert response.status_code == 201, response.text
    connector_id = UUID(response.json()["id"])
    response = await webdav_api.put(
        f"{URL}/{connector_id}/credentials",
        json={"credentials": {"client_secret": CLIENT_SECRET}},
        headers=bearer(admin_account),
    )
    assert response.status_code == 200, response.text

    response = await webdav_api.post(
        f"{URL}/{connector_id}/oauth/start", headers=bearer(account)
    )
    assert response.status_code == 200, response.text
    url = response.json()["authorize_url"]
    assert url.startswith(f"{SERVER}index.php/apps/oauth2/authorize?")
    assert CLIENT_SECRET not in url
    state = parse_qs(urlsplit(url).query)["state"][0]

    server.codes[AUTH_CODE] = IVAN
    response = await webdav_api.get(
        f"{URL}/oauth/callback", params={"code": AUTH_CODE, "state": state}
    )
    assert response.status_code == 303, response.text
    assert "status=ok" in response.headers["location"]
    with tenant_scope(require_tenant()):
        grant = (
            await session.scalars(
                select(ConnectorUserGrant)
                .options(undefer(ConnectorUserGrant.credentials))
                .where(ConnectorUserGrant.connector_id == connector_id)
            )
        ).one()
        assert grant.user_id == account_id
        assert grant.external_user_id == IVAN
        saved = SecretBox([KEY]).decrypt(grant.credentials)
        assert saved["access_token"] in server.bearer
        assert saved["user_id"] == IVAN
    # Токен проверен на сервере прямо в обратном вызове.
    assert ("PROPFIND", f"/remote.php/dav/files/{IVAN}/", "0") in server.calls
