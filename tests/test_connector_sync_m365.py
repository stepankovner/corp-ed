"""Синхронизация с адаптером Microsoft 365 (режим organization) против
поддельного Graph: файлы SharePoint и OneDrive становятся материалами с
правами по почтам, сотрудник без доступа их не видит, изменение и
удаление в источнике доходят до kronto, отказ секрета останавливает
подключение."""

from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.m365 import register
from corp_ed.connectors.registry import AdapterRegistry
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.secrets import SecretBox
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import Connector, Tenant, User, UserRole
from corp_ed.domain.types import (
    ConnectorMode,
    ConnectorStatus,
    MaterialVisibility,
    SyncRunStatus,
    SyncTrigger,
)
from corp_ed.services.connector_sync_service import ConnectorSyncService, SyncOutcome
from tests.connectors.fake_m365 import (
    CLIENT_ID,
    D_ANNA,
    D_MKT,
    D_ROOT,
    SECRET,
    TENANT_ID,
    FakeM365,
    perm_user,
    sample_m365,
)
from tests.factories import make_user
from tests.fake_connector import plain_extractor
from tests.test_connector_sync import access_of, materials_of, reload

KEY = Fernet.generate_key().decode()


def doc_id(drive: str, item: str) -> str:
    return f"m365:{drive}/{item}"


@pytest.fixture
def secrets() -> SecretBox:
    return SecretBox([KEY])


@pytest.fixture
def server() -> FakeM365:
    return sample_m365()


@pytest.fixture
def service(
    session_maker: async_sessionmaker[AsyncSession],
    server: FakeM365,
    secrets: SecretBox,
) -> ConnectorSyncService:
    settings: Any = ConnectorSettings(secrets_keys=KEY)  # type: ignore[arg-type]
    # Вид ещё не в default_registry (подключит интегратор): свой реестр.
    registry = AdapterRegistry(enabled_preview_kinds=frozenset({"m365"}))
    register(registry, settings)
    return ConnectorSyncService(
        session_maker,
        server.client(),
        registry,
        secrets,
        settings,
        extractor=plain_extractor,
    )


async def _user(session: AsyncSession, email: str) -> User:
    user = make_user(
        email=email, full_name=email, role=UserRole.EMPLOYEE, hashed_password="x"
    )
    session.add(user)
    await session.commit()
    return user


@pytest.fixture
async def anna(session: AsyncSession, tenant_ctx: Tenant) -> User:
    return await _user(session, "anna@contoso.ru")


@pytest.fixture
async def boris(session: AsyncSession, tenant_ctx: Tenant) -> User:
    return await _user(session, "boris@contoso.ru")


@pytest.fixture
async def vera(session: AsyncSession, tenant_ctx: Tenant) -> User:
    return await _user(session, "vera@contoso.ru")


async def make_connector(
    session: AsyncSession,
    secrets: SecretBox,
    *,
    modules: list[str],
    secret: str = SECRET,
) -> Connector:
    connector = Connector(
        kind="m365",
        name="Microsoft 365",
        mode=ConnectorMode.ORGANIZATION.value,
        modules=modules,
        config={"tenant_id": TENANT_ID, "client_id": CLIENT_ID},
        credentials=secrets.encrypt({"client_secret": secret}),
        credentials_set_at=datetime.now(UTC),
    )
    session.add(connector)
    await session.commit()
    return connector


async def run(service: ConnectorSyncService, connector: Connector) -> SyncOutcome:
    outcome = await service.run(
        connector.tenant_id, connector.id, trigger=SyncTrigger.MANUAL
    )
    assert outcome is not None
    return outcome


async def test_sharepoint_files_become_restricted_materials(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    anna: User,
    boris: User,
    vera: User,
    secrets: SecretBox,
    server: FakeM365,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, modules=["sharepoint"])

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED, outcome
    assert outcome.stats.added == 6
    assert outcome.stats.skipped_formats == {".png": 1}
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        plan = materials[doc_id(D_MKT, "f-plan")]
        assert plan.content == "План запуска — в мае."
        assert plan.visibility == MaterialVisibility.RESTRICTED.value
        assert plan.source_filename == "План.txt"
        assert await access_of(session, plan) == {anna.id, boris.id}
        # Анонимная ссылка выдана Вере — больше никому.
        secret = materials[doc_id(D_MKT, "f-secret")]
        assert await access_of(session, secret) == {vera.id}
        assert await access_of(session, materials[doc_id(D_ROOT, "f-rules")]) == {
            vera.id
        }
        # «Все, кроме внешних» не превращается в «вся компания».
        news = materials[doc_id(D_ROOT, "f-all")]
        assert news.visibility == MaterialVisibility.RESTRICTED.value
        assert await access_of(session, news) == set()
        # Сотрудник без доступа в источнике не видит ни одного документа.
        for material in materials.values():
            assert material.visibility == MaterialVisibility.RESTRICTED.value
            assert employee.id not in await access_of(session, material)


async def test_changes_and_deletions_follow_the_source(
    session: AsyncSession,
    tenant_ctx: Tenant,
    anna: User,
    boris: User,
    secrets: SecretBox,
    server: FakeM365,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(
        session, secrets, modules=["sharepoint", "onedrive"]
    )
    first = await run(service, connector)
    assert first.status is SyncRunStatus.SUCCEEDED, first

    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        shared = materials[doc_id(D_ANNA, "f-for-boris")]
        assert await access_of(session, shared) == {anna.id, boris.id}
        assert await access_of(session, materials[doc_id(D_ANNA, "f-draft")]) == {
            anna.id
        }

    # Между запусками: план переписан, Анна закрыла файл от Бориса,
    # бюджет удалён в SharePoint.
    server.touch(D_MKT, "f-plan", "План запуска — в июне.".encode(), etag="2")
    item = server.drives[D_ANNA]["items"]["f-for-boris"]
    item["_perms"] = [
        perm_user(server, "a0000000-0000-4000-8000-000000000001", "owner")
    ]
    server.delete_item(D_MKT, "f-budget")

    second = await run(service, connector)

    assert second.status is SyncRunStatus.SUCCEEDED, second
    assert second.stats.updated == 1
    assert second.stats.removed == 1
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        assert doc_id(D_MKT, "f-budget") not in materials
        plan = materials[doc_id(D_MKT, "f-plan")]
        assert plan.content == "План запуска — в июне."
        shared = materials[doc_id(D_ANNA, "f-for-boris")]
        assert await access_of(session, shared) == {anna.id}


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        ({"secret": "rotated"}, "client_secret_invalid"),
        ({"secret_expired": True}, "client_secret_expired"),
    ],
)
async def test_rejected_secret_stops_connector(
    session: AsyncSession,
    tenant_ctx: Tenant,
    secrets: SecretBox,
    server: FakeM365,
    service: ConnectorSyncService,
    setup: dict[str, Any],
    code: str,
) -> None:
    for name, value in setup.items():
        setattr(server, name, value)
    connector = await make_connector(session, secrets, modules=["sharepoint"])

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.FAILED
    assert outcome.error_code == code
    with tenant_scope(tenant_ctx.id):
        reloaded = await reload(session, connector)
        assert reloaded.status == ConnectorStatus.ERROR.value
        assert reloaded.last_error_code == code
