"""Синхронизация с адаптером MediaWiki (режим organization) против
поддельной вики: страницы и файлы появляются, видны только сопоставленным
читателям вики, права меняются без новой версии, правка и удаление в
источнике доходят до kronto, Lockdown без списка пространств
останавливает подключение."""

from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.mediawiki import register
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
from tests.connectors.fake_mediawiki import (
    BOT,
    BOT_PASSWORD,
    MAPPING,
    FakeMediaWiki,
    sample_wiki,
)
from tests.factories import make_user
from tests.fake_connector import plain_extractor
from tests.test_connector_sync import access_of, materials_of, reload

KEY = Fernet.generate_key().decode()


@pytest.fixture
def secrets() -> SecretBox:
    return SecretBox([KEY])


@pytest.fixture
def server() -> FakeMediaWiki:
    return sample_wiki(private=True)


@pytest.fixture
def service(
    session_maker: async_sessionmaker[AsyncSession],
    server: FakeMediaWiki,
    secrets: SecretBox,
) -> ConnectorSyncService:
    settings: Any = ConnectorSettings(secrets_keys=KEY)  # type: ignore[arg-type]
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
    """Сопоставлена, но заблокирована в вики."""
    return await _user(session, "vera@example.com")


@pytest.fixture
async def gleb(session: AsyncSession, tenant_ctx: Tenant) -> User:
    """Сопоставлен, но права read в вики у него нет."""
    return await _user(session, "gleb@example.com")


async def make_connector(
    session: AsyncSession,
    secrets: SecretBox,
    server: FakeMediaWiki,
    *,
    namespaces: str = "",
) -> Connector:
    config = {"api_url": server.api, "user_emails": MAPPING}
    if namespaces:
        config["namespaces"] = namespaces
    connector = Connector(
        kind="mediawiki",
        name="mediawiki",
        mode=ConnectorMode.ORGANIZATION.value,
        modules=["pages", "files"],
        config=config,
        credentials=secrets.encrypt({"username": BOT, "password": BOT_PASSWORD}),
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


def page_id(server: FakeMediaWiki, title: str) -> int:
    return next(p["pageid"] for p in server.pages.values() if p["title"] == title)


async def test_pages_and_files_reach_only_wiki_readers(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    anna: User,
    vera: User,
    gleb: User,
    secrets: SecretBox,
    server: FakeMediaWiki,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, server)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED, outcome
    assert outcome.stats.added == 4
    vacation = f"page:{page_id(server, 'Отпуск')}"
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        assert vacation in materials
        assert {m.title for m in materials.values()} == {
            "Отпуск",
            "Больничный",
            "Регламент.txt",
            "Памятка.md",
        }
        material = materials[vacation]
        assert material.visibility == MaterialVisibility.RESTRICTED.value
        assert "28 дней" in material.content
        assert material.content.startswith("# Отпуск")
        for found in materials.values():
            # Сотрудник без сопоставления, заблокированная Vera и Gleb без
            # права чтения — не видят; Anna — видит.
            assert await access_of(session, found) == {anna.id}, found.title


async def test_rights_edits_and_deletion_follow_the_wiki(
    session: AsyncSession,
    tenant_ctx: Tenant,
    anna: User,
    vera: User,
    secrets: SecretBox,
    server: FakeMediaWiki,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, server)
    await run(service, connector)

    server.edit(page_id(server, "Отпуск"), "<p>Теперь 31 день.</p>")
    del server.pages[page_id(server, "Больничный")]
    # Блокировку Vera сняли — доступ без новой версии страниц.
    server.add_user("Vera")
    # У Anna отняли право чтения — доступ пропадает.
    server.add_user("Anna", read=False)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED, outcome
    assert outcome.stats.updated == 1
    assert outcome.stats.removed == 1
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        titles = {m.title: m for m in materials.values()}
        assert "Больничный" not in titles
        assert "31 день" in titles["Отпуск"].content
        for found in materials.values():
            assert await access_of(session, found) == {vera.id}, found.title


async def test_lockdown_without_namespaces_stops_the_connector(
    session: AsyncSession,
    tenant_ctx: Tenant,
    anna: User,
    secrets: SecretBox,
    service: ConnectorSyncService,
    server: FakeMediaWiki,
) -> None:
    server.extensions.append("Lockdown")
    connector = await make_connector(session, secrets, server)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.FAILED
    assert outcome.error_code == "namespaces_required"
    with tenant_scope(tenant_ctx.id):
        assert await materials_of(session, connector) == {}
        reloaded = await reload(session, connector)
        assert reloaded.status == ConnectorStatus.ERROR.value


async def test_lockdown_with_listed_namespaces_syncs_them_only(
    session: AsyncSession,
    tenant_ctx: Tenant,
    anna: User,
    secrets: SecretBox,
    service: ConnectorSyncService,
    server: FakeMediaWiki,
) -> None:
    server.extensions.append("Lockdown")
    connector = await make_connector(session, secrets, server, namespaces="0")

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED, outcome
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        assert {m.title for m in materials.values()} == {"Отпуск", "Больничный"}
