"""API коннекторов для видов Kaiten, Outline и Yonote с настоящим реестром:
каталог (только когда вид включён), форма, учётные данные, проверка."""

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
from corp_ed.core.outbound import OutboundClient
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
from tests.connectors.fake_kaiten import ANNA_TOKEN, FakeKaiten, sample_kaiten
from tests.fake_connector import public_resolver

URL = "/api/v1/connectors"
KEY = Fernet.generate_key().decode()
PREVIEW = "kaiten,outline,yonote"


@pytest.fixture
def kaiten() -> FakeKaiten:
    return sample_kaiten()


@pytest.fixture
def preview_kinds() -> str:
    return PREVIEW


@pytest.fixture
async def kinds_api(
    api: httpx.AsyncClient,
    session: AsyncSession,
    kaiten: FakeKaiten,
    preview_kinds: str,
) -> AsyncGenerator[httpx.AsyncClient]:
    settings = ConnectorSettings(  # type: ignore[arg-type]
        secrets_keys=KEY, preview_kinds=preview_kinds
    )
    registry = default_registry(settings)
    secrets = SecretBox([KEY])

    def route(request: httpx.Request) -> httpx.Response:
        return kaiten.handle(request)

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
            OutboundClient(
                httpx.AsyncClient(transport=httpx.MockTransport(route)),
                resolver=public_resolver,
            ),
            resolver=public_resolver,
        )

    app.dependency_overrides[get_connector_service] = dependency
    yield api
    app.dependency_overrides.pop(get_connector_service, None)


async def _kinds(api: httpx.AsyncClient, admin: User) -> dict[str, dict[str, object]]:
    response = await api.get(f"{URL}/kinds", headers=bearer(admin))
    assert response.status_code == 200
    return {k["kind"]: k for k in response.json()}


@pytest.mark.parametrize("preview_kinds", [""])
async def test_unverified_kinds_are_not_offered(
    kinds_api: httpx.AsyncClient, admin_account: User
) -> None:
    kinds = await _kinds(kinds_api, admin_account)
    assert not {"kaiten", "outline", "yonote"} & set(kinds)
    response = await kinds_api.post(
        URL,
        json={
            "kind": "kaiten",
            "name": "Kaiten",
            "modules": ["documents"],
            "config": {"base_url": "https://company.kaiten.ru"},
        },
        headers=bearer(admin_account),
    )
    assert response.status_code == 422


async def test_kaiten_form_and_employee_token(
    kinds_api: httpx.AsyncClient,
    admin_account: User,
    account: User,
    kaiten: FakeKaiten,
) -> None:
    spec = (await _kinds(kinds_api, admin_account))["kaiten"]
    assert spec["mode"] == "per_user"
    assert [m["name"] for m in spec["modules"]] == ["documents", "card_files"]  # type: ignore[index]
    assert spec["credential_fields"] == [
        {
            "name": "token",
            "title": "Ваш API-токен Kaiten (профиль → API-ключ)",
            "required": True,
            "secret": True,
        }
    ]

    bad = await kinds_api.post(
        URL,
        json={
            "kind": "kaiten",
            "name": "Kaiten",
            "modules": ["card_files"],
            "config": {"base_url": kaiten.base, "spaces": "1,два"},
        },
        headers=bearer(admin_account),
    )
    assert bad.status_code == 422
    assert bad.json()["code"] == "spaces_invalid"
    created = await kinds_api.post(
        URL,
        json={
            "kind": "kaiten",
            "name": "Kaiten",
            "modules": ["documents", "card_files"],
            "config": {"base_url": kaiten.base},
        },
        headers=bearer(admin_account),
    )
    assert created.status_code == 201, created.text
    connector_id = created.json()["id"]

    # Токен вводит сотрудник, а не админ: учётных данных подключения нет.
    refused = await kinds_api.put(
        f"{URL}/{connector_id}/credentials",
        json={"credentials": {"token": ANNA_TOKEN}},
        headers=bearer(admin_account),
    )
    assert refused.status_code == 409

    for user, token in ((account, "wrong"), (admin_account, ANNA_TOKEN)):
        saved = await kinds_api.put(
            f"{URL}/{connector_id}/mine",
            json={"credentials": {"token": token}},
            headers=bearer(user),
        )
        assert saved.status_code == 204, saved.text
    check = await kinds_api.post(
        f"{URL}/{connector_id}/test", headers=bearer(admin_account)
    )
    assert check.json() == {"ok": True, "error_code": None}
    assert ANNA_TOKEN not in check.text
