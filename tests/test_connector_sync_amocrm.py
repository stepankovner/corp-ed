"""Синхронизация с адаптером amoCRM (режим per_user, OAuth) против
поддельного аккаунта: каждый видит файлы своих сущностей, закрытый
доступ снимает материал, изменения и удаления в источнике, новая пара
токенов сохраняется в грант, мёртвый токен гасит только свой грант."""

from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import undefer

from corp_ed.connectors.amocrm import register
from corp_ed.connectors.registry import AdapterRegistry
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.secrets import SecretBox
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import Connector, ConnectorUserGrant, Tenant, User, UserRole
from corp_ed.domain.types import (
    ConnectorMode,
    ConnectorStatus,
    GrantStatus,
    MaterialVisibility,
    SyncRunStatus,
    SyncTrigger,
)
from corp_ed.services.connector_sync_service import ConnectorSyncService, SyncOutcome
from tests.connectors.fake_amocrm import (
    ACCOUNT,
    ANNA_ACCESS,
    ANNA_REFRESH,
    BORIS_ACCESS,
    BORIS_ID,
    BORIS_REFRESH,
    CLIENT_ID,
    CLIENT_SECRET,
    NOW,
    REDIRECT_URI,
    FakeAmo,
    sample_amo,
)
from tests.factories import make_user
from tests.fake_connector import plain_extractor
from tests.test_connector_sync import access_of, materials_of, reload

KEY = Fernet.generate_key().decode()
ALL_MODULES = ["lead_files", "contacts_files", "companies_files"]


@pytest.fixture
def secrets() -> SecretBox:
    return SecretBox([KEY])


@pytest.fixture
def server() -> FakeAmo:
    return sample_amo()


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("corp_ed.connectors.amocrm.adapter.MIN_INTERVAL", 0.0)


@pytest.fixture
def service(
    session_maker: async_sessionmaker[AsyncSession],
    server: FakeAmo,
    secrets: SecretBox,
) -> ConnectorSyncService:
    settings: Any = ConnectorSettings(secrets_keys=KEY, oauth_callback_url=REDIRECT_URI)  # type: ignore[arg-type]
    registry = AdapterRegistry()
    register(registry, settings)
    return ConnectorSyncService(
        session_maker,
        server.client(),
        registry,
        secrets,
        settings,
        extractor=plain_extractor,
    )


@pytest.fixture
async def boris(session: AsyncSession, tenant_ctx: Tenant) -> User:
    user = make_user(
        email="boris@example.com",
        full_name="Борис",
        role=UserRole.EMPLOYEE,
        hashed_password="hashed",
    )
    session.add(user)
    await session.commit()
    return user


async def make_connector(
    session: AsyncSession, secrets: SecretBox, modules: list[str] | None = None
) -> Connector:
    connector = Connector(
        kind="amocrm",
        name="amoCRM",
        mode=ConnectorMode.PER_USER.value,
        modules=modules or ALL_MODULES,
        config={"account": ACCOUNT, "client_id": CLIENT_ID},
        credentials=secrets.encrypt({"client_secret": CLIENT_SECRET}),
        credentials_set_at=datetime.now(UTC),
    )
    session.add(connector)
    await session.commit()
    return connector


async def make_grant(
    session: AsyncSession,
    secrets: SecretBox,
    connector: Connector,
    user: User,
    access: str,
    refresh: str,
) -> ConnectorUserGrant:
    grant = ConnectorUserGrant(
        connector_id=connector.id,
        user_id=user.id,
        credentials=secrets.encrypt(
            {
                "access_token": access,
                "refresh_token": refresh,
                "expires_at": str(NOW + 86400),
            }
        ),
    )
    session.add(grant)
    await session.commit()
    return grant


async def grant_of(
    session: AsyncSession, grant: ConnectorUserGrant
) -> ConnectorUserGrant:
    result = await session.scalars(
        select(ConnectorUserGrant)
        .options(undefer(ConnectorUserGrant.credentials))
        .where(ConnectorUserGrant.id == grant.id)
        .execution_options(populate_existing=True)
    )
    return result.one()


async def run(service: ConnectorSyncService, connector: Connector) -> SyncOutcome:
    outcome = await service.run(
        connector.tenant_id, connector.id, trigger=SyncTrigger.MANUAL
    )
    assert outcome is not None
    return outcome


async def test_each_employee_sees_files_of_their_own_entities(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    boris: User,
    secrets: SecretBox,
    server: FakeAmo,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets)
    await make_grant(session, secrets, connector, employee, ANNA_ACCESS, ANNA_REFRESH)
    await make_grant(session, secrets, connector, boris, BORIS_ACCESS, BORIS_REFRESH)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED
    assert outcome.stats.grants == 2
    assert outcome.stats.added == 4
    assert outcome.stats.skipped_formats == {".png": 1}
    assert outcome.stats.too_large == 1
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        assert set(materials) == {
            "file:f-offer",
            "file:f-boris",
            "file:f-other",
            "file:f-company",
        }
        offer = materials["file:f-offer"]
        assert offer.content == "Коммерческое предложение: 100 ед."
        assert offer.source_format == "txt"
        assert offer.source_url == f"{ACCOUNT}/leads/detail/1001"
        assert offer.visibility == MaterialVisibility.RESTRICTED.value
        # Борис видит КП через контакт, хотя сделка ему закрыта.
        assert await access_of(session, offer) == {employee.id, boris.id}
        assert await access_of(session, materials["file:f-boris"]) == {
            employee.id,
            boris.id,
        }
        # Сделка другой воронки и компания Борису закрыты.
        assert await access_of(session, materials["file:f-other"]) == {employee.id}
        assert await access_of(session, materials["file:f-company"]) == {employee.id}


async def test_access_closed_in_amocrm_is_removed(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    boris: User,
    secrets: SecretBox,
    server: FakeAmo,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets)
    await make_grant(session, secrets, connector, employee, ANNA_ACCESS, ANNA_REFRESH)
    await make_grant(session, secrets, connector, boris, BORIS_ACCESS, BORIS_REFRESH)
    await run(service, connector)

    # Контакты Борису закрыли, его сделку передали Анне.
    server.rights[BORIS_ID]["contacts"] = "D"
    server.entities["leads"][1002]["responsible_user_id"] = 101

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        assert await access_of(session, materials["file:f-offer"]) == {employee.id}
        assert await access_of(session, materials["file:f-boris"]) == {employee.id}


async def test_changed_and_removed_files_follow_the_source(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    secrets: SecretBox,
    server: FakeAmo,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, ["lead_files"])
    await make_grant(session, secrets, connector, employee, ANNA_ACCESS, ANNA_REFRESH)
    await run(service, connector)

    server.new_version("f-offer", "Новое КП: 200 ед.".encode())
    del server.links[("leads", 1003)]  # файл отвязали от сделки
    server.files["f-boris"]["is_trashed"] = True  # файл в корзине

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED
    assert outcome.stats.updated == 1
    assert outcome.stats.removed == 2
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        assert set(materials) == {"file:f-offer"}
        assert materials["file:f-offer"].content == "Новое КП: 200 ед."


async def test_refreshed_tokens_are_saved_into_grant(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    secrets: SecretBox,
    server: FakeAmo,
    service: ConnectorSyncService,
) -> None:
    server.expired.add(ANNA_ACCESS)
    connector = await make_connector(session, secrets, ["lead_files"])
    grant = await make_grant(
        session, secrets, connector, employee, ANNA_ACCESS, ANNA_REFRESH
    )

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED
    with tenant_scope(tenant_ctx.id):
        saved = secrets.decrypt((await grant_of(session, grant)).credentials)
    assert saved["refresh_token"] != ANNA_REFRESH
    assert saved["refresh_token"] in server.refresh
    assert saved["access_token"] in server.access
    # Второй запуск — уже с новой парой, без продления.
    requests = len(server.token_requests)
    outcome = await run(service, connector)
    assert outcome.status is SyncRunStatus.SUCCEEDED
    assert len(server.token_requests) == requests


async def test_dead_token_expires_only_that_grant(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    boris: User,
    secrets: SecretBox,
    server: FakeAmo,
    service: ConnectorSyncService,
) -> None:
    server.expired.add(BORIS_ACCESS)
    del server.refresh[BORIS_REFRESH]
    connector = await make_connector(session, secrets, ["lead_files"])
    await make_grant(session, secrets, connector, employee, ANNA_ACCESS, ANNA_REFRESH)
    broken = await make_grant(
        session, secrets, connector, boris, BORIS_ACCESS, BORIS_REFRESH
    )

    outcome = await run(service, connector)

    assert outcome.stats.grants_expired == 1
    with tenant_scope(tenant_ctx.id):
        saved = await grant_of(session, broken)
        assert saved.status == GrantStatus.EXPIRED.value
        assert saved.error_code == "invalid_grant"
        materials = await materials_of(session, connector)
        assert await access_of(session, materials["file:f-boris"]) == {employee.id}
        assert (await reload(session, connector)).status == ConnectorStatus.ACTIVE.value


async def test_wrong_app_secret_stops_connector_and_keeps_grants(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    secrets: SecretBox,
    server: FakeAmo,
    service: ConnectorSyncService,
) -> None:
    server.expired.add(ANNA_ACCESS)
    connector = await make_connector(session, secrets, ["lead_files"])
    connector.credentials = secrets.encrypt({"client_secret": "wrong"})
    await session.commit()
    grant = await make_grant(
        session, secrets, connector, employee, ANNA_ACCESS, ANNA_REFRESH
    )

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.FAILED
    assert outcome.error_code == "invalid_client"
    with tenant_scope(tenant_ctx.id):
        assert (await reload(session, connector)).status == ConnectorStatus.ERROR.value
        assert (await grant_of(session, grant)).status == GrantStatus.ACTIVE.value
