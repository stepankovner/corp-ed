"""API коннекторов: роли, изоляция, форма, секреты только на запись,
очередь, гранты сотрудников, лимиты частоты."""

from collections.abc import AsyncGenerator
from typing import Annotated
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import Depends
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.api.v1.dependencies import (
    get_audit_repository,
    get_connector_service,
    get_session,
)
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.outbound import OutboundClient
from corp_ed.core.secrets import SecretBox
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import (
    AuditEvent,
    Connector,
    ConnectorSyncJob,
    ConnectorUserGrant,
    Material,
    MaterialAccess,
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
from tests.fake_connector import (
    FAKE_KIND,
    FAKE_PER_USER_KIND,
    FakeSource,
    make_registry,
    public_resolver,
)

URL = "/api/v1/connectors"
KEY = Fernet.generate_key().decode()
CREATE = {
    "kind": FAKE_KIND,
    "name": "Портал",
    "modules": ["docs"],
    "config": {"base_url": "https://portal.example.com/rest/"},
}


@pytest.fixture
def source() -> FakeSource:
    return FakeSource()


@pytest.fixture
def secrets() -> SecretBox:
    return SecretBox([KEY])


@pytest.fixture
def settings() -> ConnectorSettings:
    return ConnectorSettings(secrets_keys=KEY, max_per_tenant=3)  # type: ignore[arg-type]


@pytest.fixture
async def connectors_api(
    api: httpx.AsyncClient,
    session: AsyncSession,
    source: FakeSource,
    secrets: SecretBox,
    settings: ConnectorSettings,
) -> AsyncGenerator[httpx.AsyncClient]:
    registry = make_registry(source)

    def build(session: AsyncSession, audit: AuditRepository) -> ConnectorService:
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
                httpx.AsyncClient(
                    transport=httpx.MockTransport(lambda r: httpx.Response(500))
                )
            ),
            resolver=public_resolver,
        )

    def dependency(
        session: Annotated[AsyncSession, Depends(get_session)],
        audit: Annotated[AuditRepository, Depends(get_audit_repository)],
    ) -> ConnectorService:
        return build(session, audit)

    app.dependency_overrides[get_connector_service] = dependency
    yield api
    app.dependency_overrides.pop(get_connector_service, None)


async def _create(
    api: httpx.AsyncClient, user: User, body: dict[str, object] | None = None
) -> httpx.Response:
    return await api.post(URL, json=body or CREATE, headers=bearer(user))


async def _jobs(session: AsyncSession) -> list[ConnectorSyncJob]:
    return list(
        (
            await session.scalars(
                select(ConnectorSyncJob).execution_options(populate_existing=True)
            )
        ).all()
    )


# --- каталог и роли --------------------------------------------------------------


async def test_kinds_describe_the_form(
    connectors_api: httpx.AsyncClient, admin_account: User
) -> None:
    response = await connectors_api.get(f"{URL}/kinds", headers=bearer(admin_account))
    assert response.status_code == 200
    by_kind = {k["kind"]: k for k in response.json()}
    assert set(by_kind) == {FAKE_KIND, FAKE_PER_USER_KIND}
    spec = by_kind[FAKE_KIND]
    assert spec["mode"] == "organization"
    assert [m["name"] for m in spec["modules"]] == ["docs", "wiki"]
    assert {f["name"]: f["required"] for f in spec["config_fields"]} == {
        "base_url": True,
        "root": False,
    }
    assert spec["credential_fields"] == [
        {"name": "token", "title": "Токен", "required": True, "secret": True}
    ]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/kinds"),
        ("GET", ""),
        ("POST", ""),
        ("GET", f"/{uuid4()}"),
        ("PATCH", f"/{uuid4()}"),
        ("DELETE", f"/{uuid4()}"),
        ("PUT", f"/{uuid4()}/credentials"),
        ("POST", f"/{uuid4()}/test"),
        ("POST", f"/{uuid4()}/sync"),
        ("GET", f"/{uuid4()}/runs"),
    ],
)
async def test_admin_routes_are_closed_to_employees(
    connectors_api: httpx.AsyncClient, account: User, method: str, path: str
) -> None:
    response = await connectors_api.request(
        method, f"{URL}{path}", json={}, headers=bearer(account)
    )
    assert response.status_code == 403


async def test_routes_require_a_token(connectors_api: httpx.AsyncClient) -> None:
    assert (await connectors_api.get(URL)).status_code == 401
    assert (await connectors_api.get(f"{URL}/mine")).status_code == 401


# --- создание и форма -------------------------------------------------------------


async def test_admin_creates_connector_without_secrets(
    connectors_api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    response = await _create(connectors_api, admin_account)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["kind"] == FAKE_KIND and body["mode"] == "organization"
    assert body["status"] == "active" and body["credentials_set_at"] is None
    assert body["config"] == {"base_url": "https://portal.example.com/rest/"}
    assert body["sync_interval_minutes"] == 60
    assert "credentials" not in body
    # Без учётных данных синхронизация не ставится.
    assert await _jobs(session) == []
    [event] = (
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == AuditAction.CONNECTOR_CREATED.value
            )
        )
    ).all()
    assert event.target_id == body["id"] and event.actor_user_id == admin_account.id


@pytest.mark.parametrize(
    ("patch", "code"),
    [
        ({"kind": "salesforce"}, "kind_unknown"),
        ({"modules": ["crm"]}, "module_unknown"),
        (
            {"config": {"base_url": "https://portal.example.com/", "x": "1"}},
            "field_unknown",
        ),
        ({"config": {}}, "field_required"),
        ({"config": {"base_url": "http://portal.example.com/"}}, "scheme_not_https"),
        ({"config": {"base_url": "https://10.0.0.1/"}}, "address_not_public"),
        (
            {"config": {"base_url": "https://admin:pw@portal.example.com/"}},
            "credentials_in_url",
        ),
    ],
)
async def test_invalid_forms_are_rejected_with_a_code(
    connectors_api: httpx.AsyncClient,
    admin_account: User,
    patch: dict[str, object],
    code: str,
) -> None:
    response = await _create(connectors_api, admin_account, {**CREATE, **patch})
    assert response.status_code == 422, response.text
    assert response.json()["code"] == code


@pytest.mark.parametrize(
    "body",
    [
        {**CREATE, "tenant_id": str(uuid4())},
        {**CREATE, "status": "active"},
        {**CREATE, "credentials": {"token": "x"}},
        {**CREATE, "modules": []},
        {**CREATE, "sync_interval_minutes": 5},
        {**CREATE, "config": {"base_url": ["https://a.b/"]}},
    ],
)
async def test_unknown_or_malformed_fields_fail_validation(
    connectors_api: httpx.AsyncClient, admin_account: User, body: dict[str, object]
) -> None:
    assert (await _create(connectors_api, admin_account, body)).status_code == 422


async def test_per_tenant_cap_returns_409_with_code(
    connectors_api: httpx.AsyncClient, admin_account: User
) -> None:
    for _ in range(3):
        assert (await _create(connectors_api, admin_account)).status_code == 201
    response = await _create(connectors_api, admin_account)
    assert response.status_code == 409
    assert response.json()["code"] == "connector_limit"


# --- учётные данные ---------------------------------------------------------------


async def test_credentials_are_write_only_encrypted_and_queue_a_sync(
    connectors_api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    connector_id = (await _create(connectors_api, admin_account)).json()["id"]
    response = await connectors_api.put(
        f"{URL}/{connector_id}/credentials",
        json={"credentials": {"token": "super-secret-token"}},
        headers=bearer(admin_account),
    )
    assert response.status_code == 200, response.text
    assert response.json()["credentials_set_at"] is not None
    assert "super-secret-token" not in response.text

    with tenant_scope(admin_account.tenant_id):
        raw = await session.scalar(
            text("SELECT credentials FROM connectors WHERE id = :id"),
            {"id": connector_id},
        )
    assert raw and "super-secret-token" not in raw
    assert SecretBox([KEY]).decrypt(raw) == {"token": "super-secret-token"}

    [job] = await _jobs(session)
    assert str(job.connector_id) == connector_id and job.trigger == "manual"
    [event] = (
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == AuditAction.CONNECTOR_CREDENTIALS_SET.value
            )
        )
    ).all()
    assert event.details == {"fields": ["token"]}
    assert "super-secret-token" not in str(event.details)

    listed = await connectors_api.get(URL, headers=bearer(admin_account))
    assert "super-secret-token" not in listed.text
    single = await connectors_api.get(
        f"{URL}/{connector_id}", headers=bearer(admin_account)
    )
    assert "super-secret-token" not in single.text


async def test_credentials_form_is_validated(
    connectors_api: httpx.AsyncClient, admin_account: User
) -> None:
    connector_id = (await _create(connectors_api, admin_account)).json()["id"]
    for body, code in (
        ({"credentials": {"token": "x", "extra": "y"}}, "field_unknown"),
        ({"credentials": {"token": "   "}}, "field_required"),
    ):
        response = await connectors_api.put(
            f"{URL}/{connector_id}/credentials",
            json=body,
            headers=bearer(admin_account),
        )
        assert response.status_code == 422 and response.json()["code"] == code


async def test_without_encryption_key_credentials_are_refused(
    connectors_api: httpx.AsyncClient, admin_account: User, secrets: SecretBox
) -> None:
    secrets._fernet = None  # noqa: SLF001 — имитация пустого CONNECTOR_SECRETS_KEYS
    connector_id = (await _create(connectors_api, admin_account)).json()["id"]
    response = await connectors_api.put(
        f"{URL}/{connector_id}/credentials",
        json={"credentials": {"token": "x"}},
        headers=bearer(admin_account),
    )
    assert response.status_code == 503


async def test_credentials_on_per_user_connector_are_refused(
    connectors_api: httpx.AsyncClient, admin_account: User
) -> None:
    created = await _create(
        connectors_api, admin_account, {**CREATE, "kind": FAKE_PER_USER_KIND}
    )
    response = await connectors_api.put(
        f"{URL}/{created.json()['id']}/credentials",
        json={"credentials": {"token": "x"}},
        headers=bearer(admin_account),
    )
    assert response.status_code == 409


# --- проверка, синхронизация, журнал ----------------------------------------------


async def test_check_reports_a_code_without_source_details(
    connectors_api: httpx.AsyncClient, admin_account: User, source: FakeSource
) -> None:
    connector_id = (await _create(connectors_api, admin_account)).json()["id"]
    headers = bearer(admin_account)
    missing = await connectors_api.post(f"{URL}/{connector_id}/test", headers=headers)
    assert missing.json() == {"ok": False, "error_code": "credentials_missing"}

    await connectors_api.put(
        f"{URL}/{connector_id}/credentials",
        json={"credentials": {"token": "good"}},
        headers=headers,
    )
    assert (
        await connectors_api.post(f"{URL}/{connector_id}/test", headers=headers)
    ).json() == {
        "ok": True,
        "error_code": None,
    }
    source.rejected_tokens.add("good")
    assert (
        await connectors_api.post(f"{URL}/{connector_id}/test", headers=headers)
    ).json() == {
        "ok": False,
        "error_code": "auth_failed",
    }
    assert source.check_calls == ["good", "good"]


async def test_sync_now_queues_once_and_lists_runs(
    connectors_api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    connector_id = (await _create(connectors_api, admin_account)).json()["id"]
    headers = bearer(admin_account)
    first = await connectors_api.post(f"{URL}/{connector_id}/sync", headers=headers)
    assert first.status_code == 202 and first.json()["queued"] is True
    second = await connectors_api.post(f"{URL}/{connector_id}/sync", headers=headers)
    assert second.status_code == 202 and second.json()["queued"] is False
    assert len(await _jobs(session)) == 1
    runs = await connectors_api.get(f"{URL}/{connector_id}/runs", headers=headers)
    assert runs.status_code == 200 and runs.json() == []


async def test_sync_now_is_rate_limited_per_tenant(
    connectors_api: httpx.AsyncClient, admin_account: User
) -> None:
    connector_id = (await _create(connectors_api, admin_account)).json()["id"]
    headers = bearer(admin_account)
    for _ in range(12):
        assert (
            await connectors_api.post(f"{URL}/{connector_id}/sync", headers=headers)
        ).status_code == 202
    response = await connectors_api.post(f"{URL}/{connector_id}/sync", headers=headers)
    assert response.status_code == 429 and "retry-after" in response.headers


async def test_pause_resume_and_error_state(
    connectors_api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    connector_id = (await _create(connectors_api, admin_account)).json()["id"]
    headers = bearer(admin_account)
    paused = await connectors_api.patch(
        f"{URL}/{connector_id}", json={"status": "paused"}, headers=headers
    )
    assert paused.json()["status"] == "paused"
    assert (
        await connectors_api.post(f"{URL}/{connector_id}/sync", headers=headers)
    ).status_code == 409
    assert (
        await connectors_api.patch(
            f"{URL}/{connector_id}", json={"status": "error"}, headers=headers
        )
    ).status_code == 422

    with tenant_scope(admin_account.tenant_id):
        connector = (
            await session.scalars(select(Connector).where(Connector.id == connector_id))
        ).one()
        connector.status = "error"
        connector.last_error_code = "auth_failed"
        await session.commit()
    assert (
        await connectors_api.patch(
            f"{URL}/{connector_id}", json={"status": "active"}, headers=headers
        )
    ).status_code == 409
    fixed = await connectors_api.put(
        f"{URL}/{connector_id}/credentials",
        json={"credentials": {"token": "new"}},
        headers=headers,
    )
    assert (
        fixed.json()["status"] == "active" and fixed.json()["last_error_code"] is None
    )


async def test_update_revalidates_form_and_audits_changes(
    connectors_api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    connector_id = (await _create(connectors_api, admin_account)).json()["id"]
    headers = bearer(admin_account)
    response = await connectors_api.patch(
        f"{URL}/{connector_id}",
        json={
            "name": "Новый",
            "modules": ["wiki", "docs"],
            "sync_interval_minutes": 30,
        },
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Новый"
    assert response.json()["modules"] == ["docs", "wiki"]  # порядок каталога
    bad = await connectors_api.patch(
        f"{URL}/{connector_id}",
        json={"config": {"base_url": "http://x.example/"}},
        headers=headers,
    )
    assert bad.status_code == 422
    [event] = (
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == AuditAction.CONNECTOR_UPDATED.value
            )
        )
    ).all()
    assert event.details == {
        "name": "Новый",
        "modules": ["docs", "wiki"],
        "sync_interval_minutes": 30,
    }


# --- изоляция и удаление ----------------------------------------------------------


async def test_other_tenants_connector_is_invisible(
    connectors_api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    other = Tenant(id=uuid4(), company_code="other", name="Other")
    session.add(other)
    await session.commit()
    with tenant_scope(other.id):
        foreign = Connector(
            kind=FAKE_KIND,
            name="Чужой",
            mode="organization",
            modules=["docs"],
            config={},
        )
        session.add(foreign)
        await session.commit()

    headers = bearer(admin_account)
    assert (await connectors_api.get(URL, headers=headers)).json() == []
    for method, path, body in (
        ("GET", "", None),
        ("PATCH", "", {"name": "x"}),
        ("DELETE", "", None),
        ("PUT", "/credentials", {"credentials": {"token": "x"}}),
        ("POST", "/test", None),
        ("POST", "/sync", None),
        ("GET", "/runs", None),
        ("PUT", "/mine", {"credentials": {"token": "x"}}),
        ("DELETE", "/mine", None),
    ):
        response = await connectors_api.request(
            method, f"{URL}/{foreign.id}{path}", json=body, headers=headers
        )
        assert response.status_code == 404, (method, path)


async def test_delete_removes_connector_documents(
    connectors_api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    connector_id = (await _create(connectors_api, admin_account)).json()["id"]
    with tenant_scope(admin_account.tenant_id):
        session.add(
            Material(
                title="Из источника",
                content="x",
                connector_id=connector_id,
                external_id="d1",
                visibility="restricted",
            )
        )
        session.add(Material(title="Ручная", content="y"))
        await session.commit()

    response = await connectors_api.delete(
        f"{URL}/{connector_id}", headers=bearer(admin_account)
    )
    assert response.status_code == 204
    with tenant_scope(admin_account.tenant_id):
        titles = set((await session.scalars(select(Material.title))).all())
    assert titles == {"Ручная"}
    assert (await connectors_api.get(URL, headers=bearer(admin_account))).json() == []


# --- сотрудник: мои источники ------------------------------------------------------


async def test_employee_connects_and_disconnects_own_account(
    connectors_api: httpx.AsyncClient,
    admin_account: User,
    account: User,
    session: AsyncSession,
) -> None:
    created = await _create(
        connectors_api, admin_account, {**CREATE, "kind": FAKE_PER_USER_KIND}
    )
    connector_id = created.json()["id"]
    employee = bearer(account)

    mine = await connectors_api.get(f"{URL}/mine", headers=employee)
    assert mine.json() == [
        {
            "id": connector_id,
            "kind": FAKE_PER_USER_KIND,
            "name": "Портал",
            "grant_status": None,
            "grant_error_code": None,
        }
    ]

    response = await connectors_api.put(
        f"{URL}/{connector_id}/mine",
        json={"credentials": {"token": "my-token"}},
        headers=employee,
    )
    assert response.status_code == 204, response.text
    assert "my-token" not in response.text
    mine = await connectors_api.get(f"{URL}/mine", headers=employee)
    assert mine.json()[0]["grant_status"] == "active"
    with tenant_scope(account.tenant_id):
        [grant] = (await session.scalars(select(ConnectorUserGrant))).all()
        raw = await session.scalar(
            text("SELECT credentials FROM connector_user_grants WHERE id = :id"),
            {"id": grant.id},
        )
    assert raw and "my-token" not in raw
    [job] = await _jobs(session)
    assert str(job.connector_id) == connector_id

    # Отключение убирает грант и выведенные из него права.
    with tenant_scope(account.tenant_id):
        material = Material(
            title="Моё",
            content="x",
            connector_id=connector_id,
            external_id="d1",
            visibility="restricted",
        )
        session.add(material)
        await session.flush()
        session.add(MaterialAccess(material_id=material.id, user_id=account.id))
        await session.commit()
    response = await connectors_api.delete(
        f"{URL}/{connector_id}/mine", headers=employee
    )
    assert response.status_code == 204
    with tenant_scope(account.tenant_id):
        assert (await session.scalars(select(ConnectorUserGrant))).all() == []
        assert (await session.scalars(select(MaterialAccess))).all() == []
    assert (
        await connectors_api.delete(f"{URL}/{connector_id}/mine", headers=employee)
    ).status_code == 404
    events = (
        await session.scalars(
            select(AuditEvent.action).where(AuditEvent.actor_user_id == account.id)
        )
    ).all()
    assert events == [
        AuditAction.CONNECTOR_GRANT_SET.value,
        AuditAction.CONNECTOR_GRANT_REVOKED.value,
    ]


async def test_employee_cannot_grant_into_organization_connector(
    connectors_api: httpx.AsyncClient, admin_account: User, account: User
) -> None:
    connector_id = (await _create(connectors_api, admin_account)).json()["id"]
    response = await connectors_api.put(
        f"{URL}/{connector_id}/mine",
        json={"credentials": {"token": "x"}},
        headers=bearer(account),
    )
    assert response.status_code == 409
    assert (
        await connectors_api.get(f"{URL}/mine", headers=bearer(account))
    ).json() == []


async def test_check_on_kind_without_adapter_is_422_not_500(
    connectors_api: httpx.AsyncClient, admin_account: User, session: AsyncSession
) -> None:
    """Найдено живой проверкой: вид без адаптера в этой сборке ронял
    ручку проверки 500-й."""
    with tenant_scope(admin_account.tenant_id):
        connector = Connector(
            kind="gone", name="Старый", mode="organization", modules=["docs"], config={}
        )
        session.add(connector)
        await session.commit()
    response = await connectors_api.post(
        f"{URL}/{connector.id}/test", headers=bearer(admin_account)
    )
    assert response.status_code == 422
    assert response.json()["code"] == "kind_unknown"
