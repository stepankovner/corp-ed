"""Синхронизация с адаптером Outline/Yonote (режим organization) против
поддельного сервера: «вся компания» для открытых коллекций, почты
участников для закрытых, изменения, архив и удаление в источнике."""

from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.registry import default_registry
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
from tests.connectors.fake_outline import (
    API_KEY,
    MEMBER_KEY,
    FakeOutline,
    sample_outline,
)
from tests.factories import make_user
from tests.fake_connector import plain_extractor
from tests.test_connector_sync import access_of, materials_of, reload

KEY = Fernet.generate_key().decode()


@pytest.fixture
def secrets() -> SecretBox:
    return SecretBox([KEY])


@pytest.fixture
def dialect() -> str:
    return "outline"


@pytest.fixture
def server(dialect: str) -> FakeOutline:
    return sample_outline(dialect)


@pytest.fixture
def service(
    session_maker: async_sessionmaker[AsyncSession],
    server: FakeOutline,
    secrets: SecretBox,
) -> ConnectorSyncService:
    settings: Any = ConnectorSettings(secrets_keys=KEY)  # type: ignore[arg-type]
    return ConnectorSyncService(
        session_maker,
        server.client(),
        default_registry(settings),
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
    return await _user(session, "anna@example.com")


@pytest.fixture
async def vera(session: AsyncSession, tenant_ctx: Tenant) -> User:
    return await _user(session, "vera@example.com")


async def make_connector(
    session: AsyncSession,
    secrets: SecretBox,
    server: FakeOutline,
    *,
    token: str = API_KEY,
) -> Connector:
    connector = Connector(
        kind=server.dialect,
        name=server.dialect,
        mode=ConnectorMode.ORGANIZATION.value,
        modules=["documents"],
        config={"base_url": server.base},
        credentials=secrets.encrypt({"token": token}),
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


async def test_collections_become_company_or_member_materials(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    anna: User,
    vera: User,
    secrets: SecretBox,
    server: FakeOutline,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, server)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED
    assert outcome.stats.added == 4
    assert "documents.export" not in server.methods()
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        assert set(materials) == {
            "doc:d-vacation",
            "doc:d-sick",
            "doc:d-budget",
            "doc:d-bonus",
        }
        vacation = materials["doc:d-vacation"]
        assert vacation.visibility == MaterialVisibility.TENANT.value
        assert vacation.content == "# Отпуск\n\nОтпуск — 28 дней."
        assert vacation.source_format == "md"
        budget = materials["doc:d-budget"]
        assert budget.visibility == MaterialVisibility.RESTRICTED.value
        # Почта в Outline — «Anna@Example.com»: сопоставляется без регистра.
        assert await access_of(session, budget) == {anna.id}
        assert await access_of(session, materials["doc:d-bonus"]) == {anna.id, vera.id}


async def test_changes_archive_and_deletion_follow_the_source(
    session: AsyncSession,
    tenant_ctx: Tenant,
    anna: User,
    secrets: SecretBox,
    server: FakeOutline,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, server)
    await run(service, connector)

    server.documents["d-vacation"]["updatedAt"] = "2026-10-01T10:00:00.000Z"
    server.documents["d-vacation"]["revision"] = 2
    server.texts["d-vacation"] = "Теперь 31 день."
    server.documents["d-sick"]["archivedAt"] = "2026-10-01T10:00:00.000Z"
    server.documents["d-bonus"]["deletedAt"] = "2026-10-01T10:00:00.000Z"
    # Коллекцию открыли: права меняются без новой версии документа.
    server.collections["c-fin"]["permission"] = "read_write"

    outcome = await run(service, connector)

    assert outcome.stats.updated == 1
    assert outcome.stats.removed == 2
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        assert set(materials) == {"doc:d-vacation", "doc:d-budget"}
        assert materials["doc:d-vacation"].content == "# Отпуск\n\nТеперь 31 день."
        budget = materials["doc:d-budget"]
        assert budget.visibility == MaterialVisibility.TENANT.value


async def test_member_key_stops_the_connector(
    session: AsyncSession,
    tenant_ctx: Tenant,
    secrets: SecretBox,
    server: FakeOutline,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, server, token=MEMBER_KEY)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.FAILED
    assert outcome.error_code == "admin_required"
    with tenant_scope(tenant_ctx.id):
        reloaded = await reload(session, connector)
        assert reloaded.status == ConnectorStatus.ERROR.value


@pytest.mark.parametrize("dialect", ["yonote"])
async def test_yonote_uses_the_same_adapter(
    session: AsyncSession,
    tenant_ctx: Tenant,
    anna: User,
    vera: User,
    secrets: SecretBox,
    server: FakeOutline,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, server)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        assert materials["doc:d-vacation"].visibility == MaterialVisibility.TENANT.value
        # Участников документа Yonote не отдаёт: только коллекция.
        assert await access_of(session, materials["doc:d-bonus"]) == {anna.id}
