"""Синхронизация с адаптером Kaiten (режим per_user, личные токены) против
поддельного Kaiten: каждый видит своё, изменения и удаления в источнике,
отозванный токен одного сотрудника не мешает остальным."""

from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.registry import default_registry
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.secrets import SecretBox
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import Connector, ConnectorUserGrant, Tenant, User
from corp_ed.domain.types import (
    ConnectorMode,
    GrantStatus,
    MaterialVisibility,
    SyncRunStatus,
    SyncTrigger,
)
from corp_ed.services.connector_sync_service import ConnectorSyncService, SyncOutcome
from tests.connectors.fake_kaiten import (
    ANNA_TOKEN,
    BORIS_TOKEN,
    FakeKaiten,
    pm,
    sample_kaiten,
)
from tests.factories import make_user
from tests.fake_connector import plain_extractor
from tests.test_connector_sync import access_of, materials_of

KEY = Fernet.generate_key().decode()


@pytest.fixture
def secrets() -> SecretBox:
    return SecretBox([KEY])


@pytest.fixture
def server() -> FakeKaiten:
    return sample_kaiten()


def make_settings(**overrides: Any) -> ConnectorSettings:
    return ConnectorSettings(secrets_keys=KEY, **overrides)  # type: ignore[arg-type]


@pytest.fixture
def service(
    session_maker: async_sessionmaker[AsyncSession],
    server: FakeKaiten,
    secrets: SecretBox,
) -> ConnectorSyncService:
    settings = make_settings()
    return ConnectorSyncService(
        session_maker,
        server.client(),
        default_registry(settings),
        secrets,
        settings,
        extractor=plain_extractor,
    )


@pytest.fixture
async def boris(session: AsyncSession, tenant_ctx: Tenant) -> User:
    from corp_ed.domain.models import UserRole

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
    session: AsyncSession, server: FakeKaiten, modules: list[str] | None = None
) -> Connector:
    connector = Connector(
        kind="kaiten",
        name="Kaiten",
        mode=ConnectorMode.PER_USER.value,
        modules=modules or ["documents"],
        config={"base_url": server.base},
        credentials=None,
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
    token: str,
) -> ConnectorUserGrant:
    grant = ConnectorUserGrant(
        connector_id=connector.id,
        user_id=user.id,
        credentials=secrets.encrypt({"token": token}),
    )
    session.add(grant)
    await session.commit()
    return grant


async def run(service: ConnectorSyncService, connector: Connector) -> SyncOutcome:
    outcome = await service.run(
        connector.tenant_id, connector.id, trigger=SyncTrigger.MANUAL
    )
    assert outcome is not None
    return outcome


async def test_each_employee_sees_only_what_kaiten_shows_them(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    boris: User,
    secrets: SecretBox,
    server: FakeKaiten,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, server)
    await make_grant(session, secrets, connector, employee, ANNA_TOKEN)
    await make_grant(session, secrets, connector, boris, BORIS_TOKEN)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED
    assert outcome.stats.grants == 2
    assert outcome.stats.added == 5
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        vacation = materials["doc:d-vacation"]
        assert vacation.content == "# Отпуск\n\nОтпуск — 28 календарных дней."
        assert vacation.source_format == "md"
        assert vacation.source_url == f"{server.base}documents/d-vacation"
        assert vacation.visibility == MaterialVisibility.RESTRICTED.value
        assert await access_of(session, vacation) == {employee.id, boris.id}
        salary = materials["doc:d-salary"]
        assert await access_of(session, salary) == {employee.id}
        assert await access_of(session, materials["doc:d-bonus"]) == {employee.id}


async def test_changed_and_removed_documents_follow_the_source(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    secrets: SecretBox,
    server: FakeKaiten,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, server)
    await make_grant(session, secrets, connector, employee, ANNA_TOKEN)
    await run(service, connector)

    server.documents["d-vacation"].update(
        version=2, updated="2026-10-01T10:00:00.000Z", data=pm("Теперь 31 день.")
    )
    server.entities["d-root"]["archived"] = True
    del server.documents["d-bonus"]
    del server.entities["d-bonus"]

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED
    assert outcome.stats.updated == 1
    assert outcome.stats.removed == 2
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        assert set(materials) == {"doc:d-vacation", "doc:d-salary", "doc:d-inside"}
        assert materials["doc:d-vacation"].content == "# Отпуск\n\nТеперь 31 день."


async def test_rejected_token_expires_only_that_grant(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    boris: User,
    secrets: SecretBox,
    server: FakeKaiten,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, server)
    await make_grant(session, secrets, connector, employee, ANNA_TOKEN)
    broken = await make_grant(session, secrets, connector, boris, "revoked-token")

    outcome = await run(service, connector)

    assert outcome.stats.grants_expired == 1
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        assert "doc:d-salary" in materials
        assert await access_of(session, materials["doc:d-vacation"]) == {employee.id}
        grant = (
            await session.scalars(
                select(ConnectorUserGrant)
                .where(ConnectorUserGrant.id == broken.id)
                .execution_options(populate_existing=True)
            )
        ).one()
        assert grant.status == GrantStatus.EXPIRED.value


async def test_card_files_are_downloaded_and_counted(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    secrets: SecretBox,
    server: FakeKaiten,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, server, ["card_files"])
    await make_grant(session, secrets, connector, employee, ANNA_TOKEN)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED
    assert outcome.stats.skipped_formats == {".png": 1}
    assert outcome.stats.too_large == 1
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        assert set(materials) == {"file:f-guide", "file:7001", "file:f-budget"}
        guide = materials["file:f-guide"]
        assert guide.content == "Первый день: пропуск."
        assert guide.source_format == "txt"
        assert await access_of(session, guide) == {employee.id}
    assert all(auth == "" for auth in server.storage_auth)
