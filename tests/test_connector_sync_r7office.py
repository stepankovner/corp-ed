"""Синхронизация с адаптером Р7-Офис КС 2019 (режим organization) против
поддельного портала: файлы «Общих документов» с почтами читателей,
сотрудник без доступа документа не видит, изменения прав и удаления в
источнике, учётка не администратора останавливает подключение."""

from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.r7office import register
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
from tests.connectors.fake_r7office import (
    ADMIN_LOGIN,
    ADMIN_PASSWORD,
    BORIS_ID,
    EVERYONE,
    FINANCE,
    READ,
    RESTRICT,
    FakeR7,
    sample_r7,
)
from tests.factories import make_user
from tests.fake_connector import plain_extractor
from tests.test_connector_sync import access_of, materials_of, reload

KEY = Fernet.generate_key().decode()


@pytest.fixture
def secrets() -> SecretBox:
    return SecretBox([KEY])


@pytest.fixture
def server() -> FakeR7:
    return sample_r7()


@pytest.fixture
def service(
    session_maker: async_sessionmaker[AsyncSession],
    server: FakeR7,
    secrets: SecretBox,
) -> ConnectorSyncService:
    settings: Any = ConnectorSettings(secrets_keys=KEY)  # type: ignore[arg-type]
    # Свой реестр: вид подключает к default_registry интегратор.
    registry = AdapterRegistry(enabled_preview_kinds=frozenset({"r7office"}))
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
    return await _user(session, "anna@company.ru")


@pytest.fixture
async def boris(session: AsyncSession, tenant_ctx: Tenant) -> User:
    return await _user(session, "boris@company.ru")


async def make_connector(
    session: AsyncSession,
    secrets: SecretBox,
    server: FakeR7,
    *,
    login: str = ADMIN_LOGIN,
    password: str = ADMIN_PASSWORD,
) -> Connector:
    connector = Connector(
        kind="r7office",
        name="Р7",
        mode=ConnectorMode.ORGANIZATION.value,
        modules=["common"],
        config={"base_url": server.base},
        credentials=secrets.encrypt({"login": login, "password": password}),
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


async def test_common_files_carry_the_portal_rights(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    anna: User,
    boris: User,
    secrets: SecretBox,
    server: FakeR7,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, server)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED
    assert outcome.stats.added == 5
    assert outcome.stats.skipped_formats == {".png": 1}
    assert outcome.stats.too_large == 1
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        vacation = materials["file:100"]
        assert vacation.content == "Отпуск — 28 дней."
        assert vacation.source_format == "txt"
        assert vacation.visibility == MaterialVisibility.RESTRICTED.value
        # employee@test.com нет в Р7: «Все» — только люди портала.
        assert await access_of(session, vacation) == {anna.id, boris.id}
        # «Бюджет» — для «Финансов»: Борис его не видит.
        assert await access_of(session, materials["file:200"]) == {anna.id}
        assert await access_of(session, materials["file:201"]) == {boris.id}
        # Только по ссылке и администратору — никому из kronto.
        assert await access_of(session, materials["file:202"]) == set()


async def test_rights_changes_and_deletions_follow_the_source(
    session: AsyncSession,
    tenant_ctx: Tenant,
    anna: User,
    boris: User,
    secrets: SecretBox,
    server: FakeR7,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, server)
    await run(service, connector)

    # «Кадры» закрыли всем, кроме Бориса; версия файла не менялась.
    server.share("folder", 10, EVERYONE, RESTRICT)
    server.share("folder", 10, BORIS_ID, READ)
    # «Бюджет» открыли «Финансам» и Борису лично, файл обновили.
    server.share("folder", 21, BORIS_ID, READ)
    server.files[200].update(version=2, updated="2026-10-01T10:00:00.0000000Z")
    server.blobs[200] = "Смета уточнена.".encode()
    del server.files[103]
    del server.files[201]

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED
    assert outcome.stats.updated == 1
    assert outcome.stats.removed == 2
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        assert set(materials) == {"file:100", "file:200", "file:202"}
        assert await access_of(session, materials["file:100"]) == {boris.id}
        budget = materials["file:200"]
        assert budget.content == "Смета уточнена."
        assert await access_of(session, budget) == {anna.id, boris.id}


async def test_group_closed_to_everyone_hides_the_file(
    session: AsyncSession,
    tenant_ctx: Tenant,
    anna: User,
    boris: User,
    secrets: SecretBox,
    server: FakeR7,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, server)
    await run(service, connector)

    server.share("folder", 21, FINANCE, RESTRICT)

    await run(service, connector)

    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        # Читает только администратор портала (его нет в kronto) — файл
        # остаётся ему, сотрудникам — нет.
        assert await access_of(session, materials["file:200"]) == set()


async def test_non_admin_account_stops_the_connector(
    session: AsyncSession,
    tenant_ctx: Tenant,
    secrets: SecretBox,
    server: FakeR7,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(
        session, secrets, server, login="anna@company.ru", password="pw"
    )

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.FAILED
    assert outcome.error_code == "admin_required"
    with tenant_scope(tenant_ctx.id):
        reloaded = await reload(session, connector)
        assert reloaded.status == ConnectorStatus.ERROR.value
