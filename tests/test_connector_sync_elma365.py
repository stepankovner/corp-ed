"""Синхронизация с адаптером ELMA365 (режим per_user, токены сотрудников)
против поддельной ELMA365: каждый видит только прошедшее проверку прав по
своему токену, изменения и удаления в источнике, отозванный токен одного
сотрудника не мешает остальным."""

from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.elma365 import register
from corp_ed.connectors.registry import AdapterRegistry
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
from tests.connectors.fake_elma365 import (
    ANNA_TOKEN,
    BORIS_TOKEN,
    DIR_HR,
    FILE_BONUS,
    FILE_PRIVATE,
    FILE_ROOT,
    FILE_SALARY,
    FILE_VACATION,
    FakeElma365,
    sample_elma365,
)
from tests.factories import make_user
from tests.fake_connector import plain_extractor
from tests.test_connector_sync import access_of, materials_of

KEY = Fernet.generate_key().decode()


def fid(key: str) -> str:
    return f"file:{key}"


@pytest.fixture
def secrets() -> SecretBox:
    return SecretBox([KEY])


@pytest.fixture
def server() -> FakeElma365:
    return sample_elma365()


def make_settings(**overrides: Any) -> ConnectorSettings:
    return ConnectorSettings(secrets_keys=KEY, **overrides)  # type: ignore[arg-type]


@pytest.fixture
def service(
    session_maker: async_sessionmaker[AsyncSession],
    server: FakeElma365,
    secrets: SecretBox,
) -> ConnectorSyncService:
    settings = make_settings()
    registry = AdapterRegistry(enabled_preview_kinds=frozenset({"elma365"}))
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


async def make_connector(session: AsyncSession, server: FakeElma365) -> Connector:
    connector = Connector(
        kind="elma365",
        name="ELMA365",
        mode=ConnectorMode.PER_USER.value,
        modules=["files"],
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


async def test_each_employee_sees_only_what_elma365_allows_them(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    boris: User,
    secrets: SecretBox,
    server: FakeElma365,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, server)
    await make_grant(session, secrets, connector, employee, ANNA_TOKEN)
    await make_grant(session, secrets, connector, boris, BORIS_TOKEN)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED
    assert outcome.stats.grants == 2
    assert outcome.stats.added == 5
    assert outcome.stats.skipped_formats == {".png": 1}
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        vacation = materials[fid(FILE_VACATION)]
        assert vacation.content == "Отпуск — 28 календарных дней."
        assert vacation.source_format == "txt"
        assert vacation.source_url == f"{server.base}files/{DIR_HR}"
        assert vacation.visibility == MaterialVisibility.RESTRICTED.value
        assert await access_of(session, vacation) == {employee.id, boris.id}
        assert await access_of(session, materials[fid(FILE_ROOT)]) == {
            employee.id,
            boris.id,
        }
        # Поиск отдаёт Борису и закрытое — в материалы оно ему не попадает.
        for closed in (FILE_SALARY, FILE_BONUS, FILE_PRIVATE):
            assert await access_of(session, materials[fid(closed)]) == {employee.id}
    assert all(auth == "" for auth in server.storage_auth)


async def test_changed_and_removed_files_follow_the_source(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    secrets: SecretBox,
    server: FakeElma365,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, server)
    await make_grant(session, secrets, connector, employee, ANNA_TOKEN)
    await run(service, connector)

    server.update_file(
        FILE_VACATION, "Теперь 31 день.".encode(), updated="2026-10-01T10:00:00Z"
    )
    del server.files[FILE_BONUS]
    server.file_readers[FILE_ROOT] = {"someone-else"}

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED
    assert outcome.stats.updated == 1
    assert outcome.stats.removed == 2
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        assert set(materials) == {
            fid(FILE_VACATION),
            fid(FILE_SALARY),
            fid(FILE_PRIVATE),
        }
        assert materials[fid(FILE_VACATION)].content == "Теперь 31 день."


async def test_rejected_token_expires_only_that_grant(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    boris: User,
    secrets: SecretBox,
    server: FakeElma365,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, server)
    await make_grant(session, secrets, connector, employee, ANNA_TOKEN)
    broken = await make_grant(session, secrets, connector, boris, "revoked-token")

    outcome = await run(service, connector)

    assert outcome.stats.grants_expired == 1
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        assert fid(FILE_SALARY) in materials
        assert await access_of(session, materials[fid(FILE_VACATION)]) == {employee.id}
        grant = (
            await session.scalars(
                select(ConnectorUserGrant)
                .where(ConnectorUserGrant.id == broken.id)
                .execution_options(populate_existing=True)
            )
        ).one()
        assert grant.status == GrantStatus.EXPIRED.value
