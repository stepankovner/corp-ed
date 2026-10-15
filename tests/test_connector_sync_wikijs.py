"""Синхронизация с адаптером Wiki.js (режим organization) против
поддельного сервера: «вся компания» для страниц гостей, почты для
закрытых правилами групп, правка, смена правил и удаление в источнике."""

from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.registry import AdapterRegistry
from corp_ed.connectors.wikijs import register
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
from tests.connectors.fake_wikijs import API_KEY, MEMBER_KEY, FakeWikiJs, sample_wikijs
from tests.factories import make_user
from tests.fake_connector import plain_extractor
from tests.test_connector_sync import access_of, materials_of, reload

KEY = Fernet.generate_key().decode()


@pytest.fixture
def secrets() -> SecretBox:
    return SecretBox([KEY])


@pytest.fixture
def server() -> FakeWikiJs:
    return sample_wikijs()


@pytest.fixture
def service(
    session_maker: async_sessionmaker[AsyncSession],
    server: FakeWikiJs,
    secrets: SecretBox,
) -> ConnectorSyncService:
    settings: Any = ConnectorSettings(secrets_keys=KEY)  # type: ignore[arg-type]
    registry = AdapterRegistry(enabled_preview_kinds=frozenset({"wikijs"}))
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
async def people(session: AsyncSession, tenant_ctx: Tenant) -> dict[str, User]:
    """ivan, anna, petr — сотрудники Wiki.js; olga там отключена."""
    return {
        login: await _user(session, f"{login}@example.com")
        for login in ("ivan", "anna", "petr", "olga")
    }


async def make_connector(
    session: AsyncSession, secrets: SecretBox, server: FakeWikiJs, token: str = API_KEY
) -> Connector:
    connector = Connector(
        kind="wikijs",
        name="Wiki.js",
        mode=ConnectorMode.ORGANIZATION.value,
        modules=["pages"],
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


def ids(people: dict[str, User], *logins: str) -> set[Any]:
    return {people[login].id for login in logins}


async def test_pages_become_company_or_group_materials(
    session: AsyncSession,
    tenant_ctx: Tenant,
    people: dict[str, User],
    secrets: SecretBox,
    server: FakeWikiJs,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, server)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED
    assert outcome.stats.added == 9
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        assert set(materials) == {f"page:{n}" for n in range(1, 10)}
        welcome = materials["page:1"]
        assert welcome.visibility == MaterialVisibility.TENANT.value
        assert "Открыто без входа." in welcome.content
        assert "¶" not in welcome.content
        salaries = materials["page:3"]
        assert salaries.visibility == MaterialVisibility.RESTRICTED.value
        # Почта в Wiki.js — «anna@Example.com»: сопоставляется без регистра.
        assert await access_of(session, salaries) == ids(people, "anna")
        assert await access_of(session, materials["page:4"]) == ids(
            people, "ivan", "anna"
        )
        # Тег confidential закрыт всем, кроме администратора Wiki.js.
        assert await access_of(session, materials["page:5"]) == set()
        assert await access_of(session, materials["page:2"]) == ids(
            people, "ivan", "anna", "petr"
        )
        assert await access_of(session, materials["page:8"]) == set()


async def test_edits_rules_and_deletion_follow_the_source(
    session: AsyncSession,
    tenant_ctx: Tenant,
    people: dict[str, User],
    secrets: SecretBox,
    server: FakeWikiJs,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, server)
    await run(service, connector)

    server.pages[2]["updatedAt"] = "2026-10-15T10:00:00.000Z"
    server.pages[2]["render"] = "<h1>Отпуск</h1><p>Теперь 31 день.</p>"
    del server.pages[7]
    server.pages[9]["isPublished"] = False
    # anna вышла из «Кадров»: зарплаты закрываются без новой версии.
    server.members[4].discard(4)
    # Гостям открыли отчёты: страница становится общей.
    server.groups[2]["pageRules"].append(
        {**server.groups[2]["pageRules"][0], "path": "reports"}
    )

    outcome = await run(service, connector)

    assert outcome.stats.updated == 1
    assert outcome.stats.removed == 2
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        assert "page:7" not in materials and "page:9" not in materials
        assert "Теперь 31 день." in materials["page:2"].content
        assert await access_of(session, materials["page:3"]) == set()
        assert materials["page:6"].visibility == MaterialVisibility.TENANT.value


async def test_group_key_stops_the_connector(
    session: AsyncSession,
    tenant_ctx: Tenant,
    secrets: SecretBox,
    server: FakeWikiJs,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, server, token=MEMBER_KEY)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.FAILED
    assert outcome.error_code == "admin_required"
    with tenant_scope(tenant_ctx.id):
        reloaded = await reload(session, connector)
        assert reloaded.status == ConnectorStatus.ERROR.value
