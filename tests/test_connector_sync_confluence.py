"""Синхронизация с адаптером Confluence против поддельного сервера: режим
organization, видимость компании и ограниченные страницы по почтам."""

from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.registry import default_registry
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.secrets import SecretBox
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import Connector, Tenant, User
from corp_ed.domain.types import (
    ConnectorMode,
    ConnectorStatus,
    MaterialVisibility,
    SyncRunStatus,
    SyncTrigger,
)
from corp_ed.services.connector_sync_service import ConnectorSyncService, SyncOutcome
from tests.connectors.fake_confluence import PAT, FakeConfluence, sample_confluence
from tests.fake_connector import plain_extractor
from tests.test_connector_sync import access_of, materials_of, reload

KEY = Fernet.generate_key().decode()


@pytest.fixture
def secrets() -> SecretBox:
    return SecretBox([KEY])


@pytest.fixture
def server() -> FakeConfluence:
    return sample_confluence()


def make_settings(**overrides: Any) -> ConnectorSettings:
    return ConnectorSettings(secrets_keys=KEY, **overrides)  # type: ignore[arg-type]


@pytest.fixture
def service(
    session_maker: async_sessionmaker[AsyncSession],
    server: FakeConfluence,
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


async def make_connector(
    session: AsyncSession,
    secrets: SecretBox,
    server: FakeConfluence,
    *,
    modules: list[str] | None = None,
    token: str = PAT,
    email_template: str = "{username}@example.com",
) -> Connector:
    connector = Connector(
        kind="confluence",
        name="Confluence",
        mode=ConnectorMode.ORGANIZATION.value,
        modules=modules or ["pages"],
        config={
            "base_url": server.base,
            "spaces": "HR",
            "email_template": email_template,
        },
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


@pytest.fixture
async def anna(session: AsyncSession, tenant_ctx: Tenant) -> User:
    from corp_ed.core.security import hash_password
    from corp_ed.domain.models import UserRole

    user = User(
        email="anna@example.com",
        full_name="Анна",
        role=UserRole.EMPLOYEE,
        hashed_password=hash_password("Password-1234"),
    )
    session.add(user)
    await session.commit()
    return user


async def test_pages_become_company_or_restricted_materials(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    anna: User,
    secrets: SecretBox,
    server: FakeConfluence,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, server)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.PARTIAL  # пустая страница
    assert outcome.stats.added == 3
    assert outcome.stats.failed == 1
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        assert set(materials) == {"page:100", "page:101", "page:102"}
        vacation = materials["page:100"]
        assert vacation.visibility == MaterialVisibility.TENANT.value
        assert "28 календарных дней" in vacation.content
        assert vacation.source_url == f"{server.base}display/HR/Отпуск"
        assert vacation.source_format == "html"
        assert await access_of(session, vacation) == set()
        salaries = materials["page:101"]
        assert salaries.visibility == MaterialVisibility.RESTRICTED.value
        # anna есть у нас, boris — нет: доступ только у Анны.
        assert await access_of(session, salaries) == {anna.id}
        bonuses = materials["page:102"]
        assert await access_of(session, bonuses) == set()
        assert (await reload(session, connector)).status == ConnectorStatus.ACTIVE.value


async def test_attachments_module_downloads_files(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    anna: User,
    secrets: SecretBox,
    server: FakeConfluence,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, server, modules=["attachments"])

    outcome = await run(service, connector)

    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        # docx через plain_extractor не разобрать (тест ядра, не песочницы) —
        # он считается упавшим; txt читается.
        assert "att:503" in materials
        sheet = materials["att:503"]
        assert sheet.content == "секретная ведомость"
        assert sheet.source_format == "txt"
        assert sheet.visibility == MaterialVisibility.RESTRICTED.value
        assert await access_of(session, sheet) == {anna.id}
    assert outcome.stats.seen == 2


async def test_rejected_token_stops_connector(
    session: AsyncSession,
    tenant_ctx: Tenant,
    secrets: SecretBox,
    server: FakeConfluence,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, server, token="wrong")

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.FAILED
    assert outcome.error_code == "anonymous"
    with tenant_scope(tenant_ctx.id):
        reloaded = await reload(session, connector)
        assert reloaded.status == ConnectorStatus.ERROR.value
        assert reloaded.last_error_code == "anonymous"
