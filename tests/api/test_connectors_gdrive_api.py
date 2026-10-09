"""Google Drive через API подключений: вид скрыт до включения, форма,
JSON-ключ длиннее обычного поля, проверка доступа кодом."""

from collections.abc import AsyncGenerator
from typing import Annotated

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.api.v1.dependencies import (
    get_audit_repository,
    get_connector_service,
    get_session,
)
from corp_ed.connectors.registry import default_registry
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.secrets import SecretBox
from corp_ed.domain.models import User
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
from tests.connectors.fake_google import (
    ADMIN,
    DIRECTORY_API,
    DOMAIN,
    DRIVE_API,
    GROUPS_SCOPE,
    TOKEN_URL,
    FakeGoogle,
    sample_google,
    service_account_key,
)
from tests.fake_connector import public_resolver

URL = "/api/v1/connectors"
KEY = Fernet.generate_key().decode()
CREATE = {
    "kind": "gdrive",
    "name": "Google Диск",
    "modules": ["shared_drives", "user_drives"],
    "config": {"domains": DOMAIN, "admin_email": ADMIN},
}


@pytest.fixture
def google() -> FakeGoogle:
    return sample_google()


@pytest.fixture
def preview_kinds() -> str:
    return "gdrive"


@pytest.fixture
async def gdrive_api(
    api: httpx.AsyncClient, google: FakeGoogle, preview_kinds: str
) -> AsyncGenerator[httpx.AsyncClient]:
    settings = ConnectorSettings(
        secrets_keys=KEY,
        preview_kinds=preview_kinds,
        google_token_url=TOKEN_URL,
        google_drive_api=DRIVE_API,
        google_directory_api=DIRECTORY_API,
    )  # type: ignore[arg-type]
    registry = default_registry(settings)

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
            SecretBox([KEY]),
            registry,
            settings,
            session,
            google.client(),
            resolver=public_resolver,
        )

    app.dependency_overrides[get_connector_service] = dependency
    yield api
    app.dependency_overrides.pop(get_connector_service, None)


@pytest.mark.parametrize("preview_kinds", [""])
async def test_gdrive_is_not_offered_until_enabled(
    gdrive_api: httpx.AsyncClient, admin_account: User
) -> None:
    kinds = await gdrive_api.get(f"{URL}/kinds", headers=bearer(admin_account))
    assert "gdrive" not in [k["kind"] for k in kinds.json()]
    created = await gdrive_api.post(URL, json=CREATE, headers=bearer(admin_account))
    assert created.status_code == 422
    assert created.json()["code"] == "kind_unknown"


async def test_admin_connects_google_drive(
    gdrive_api: httpx.AsyncClient, admin_account: User, google: FakeGoogle
) -> None:
    headers = bearer(admin_account)
    kinds = await gdrive_api.get(f"{URL}/kinds", headers=headers)
    spec = {k["kind"]: k for k in kinds.json()}["gdrive"]
    assert spec["mode"] == "organization"
    assert [f["name"] for f in spec["config_fields"]] == ["domains", "admin_email"]
    assert spec["credential_fields"] == [
        {
            "name": "service_account_key",
            "title": "JSON-ключ сервисного аккаунта с делегированием на домен",
            "required": True,
            "secret": True,
        }
    ]
    assert GROUPS_SCOPE in spec["extra"]["scopes"]

    created = await gdrive_api.post(URL, json=CREATE, headers=headers)
    assert created.status_code == 201, created.text
    connector_id = created.json()["id"]

    key = service_account_key()
    assert len(key) > 2048
    stored = await gdrive_api.put(
        f"{URL}/{connector_id}/credentials",
        json={"credentials": {"service_account_key": key}},
        headers=headers,
    )
    assert stored.status_code == 200, stored.text
    assert "PRIVATE KEY" not in stored.text

    checked = await gdrive_api.post(f"{URL}/{connector_id}/test", headers=headers)
    assert checked.json() == {"ok": True, "error_code": None}

    google.delegated.discard(GROUPS_SCOPE)
    checked = await gdrive_api.post(f"{URL}/{connector_id}/test", headers=headers)
    assert checked.json() == {
        "ok": False,
        "error_code": "delegation_missing_groups",
    }


async def test_gdrive_form_is_validated(
    gdrive_api: httpx.AsyncClient, admin_account: User
) -> None:
    headers = bearer(admin_account)
    foreign_admin = {
        **CREATE,
        "config": {"domains": DOMAIN, "admin_email": "admin@other.org"},
    }
    response = await gdrive_api.post(URL, json=foreign_admin, headers=headers)
    assert response.status_code == 422
    assert response.json()["code"] == "admin_email_domain_mismatch"

    connector_id = (await gdrive_api.post(URL, json=CREATE, headers=headers)).json()[
        "id"
    ]
    for value, code in (
        ('{"type": "authorized_user"}', "service_account_key_invalid"),
        ("x" * 9000, "field_invalid"),
    ):
        response = await gdrive_api.put(
            f"{URL}/{connector_id}/credentials",
            json={"credentials": {"service_account_key": value}},
            headers=headers,
        )
        assert response.status_code == 422
        assert response.json()["code"] == code
