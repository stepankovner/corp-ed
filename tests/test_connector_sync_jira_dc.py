"""Синхронизация с адаптером Jira Server/DC (режим organization) против
поддельного сервера: задачи и вложения становятся материалами с почтами
читателей, сотрудник без доступа их не видит, отзыв прав и удаление в
Jira доходят до kronto."""

from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.jira_dc import KIND, register
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
from tests.connectors.fake_jira_dc import (
    ADMIN_TOKEN,
    MEMBER_TOKEN,
    FakeJira,
    sample_jira,
)
from tests.factories import make_user
from tests.fake_connector import plain_extractor
from tests.test_connector_sync import access_of, materials_of, reload

KEY = Fernet.generate_key().decode()


@pytest.fixture
def secrets() -> SecretBox:
    return SecretBox([KEY])


@pytest.fixture
def server() -> FakeJira:
    return sample_jira()


@pytest.fixture
def service(
    session_maker: async_sessionmaker[AsyncSession],
    server: FakeJira,
    secrets: SecretBox,
) -> ConnectorSyncService:
    settings: Any = ConnectorSettings(secrets_keys=KEY)  # type: ignore[arg-type]
    # Вид ещё не в default_registry (подключит интегратор): свой реестр.
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
async def people(session: AsyncSession, tenant_ctx: Tenant) -> dict[str, User]:
    """Сотрудники kronto с теми же почтами, что в Jira (и erin — она в
    Jira деактивирована)."""
    return {
        name: await _user(session, f"{name}@example.com")
        for name in ("alice", "bob", "carol", "dave", "erin")
    }


async def make_connector(
    session: AsyncSession,
    secrets: SecretBox,
    server: FakeJira,
    *,
    token: str = ADMIN_TOKEN,
    modules: tuple[str, ...] = ("issues", "attachments"),
) -> Connector:
    connector = Connector(
        kind=KIND,
        name="Jira",
        mode=ConnectorMode.ORGANIZATION.value,
        modules=list(modules),
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


def _by_title(materials: dict[str, Any]) -> dict[str, Any]:
    return {m.title: m for m in materials.values()}


async def test_issues_become_materials_for_their_jira_readers(
    session: AsyncSession,
    tenant_ctx: Tenant,
    people: dict[str, User],
    secrets: SecretBox,
    server: FakeJira,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, server)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED
    # Пять задач, видимых владельцу токена, и вложение .md (.exe пропущен).
    assert outcome.stats.added == 6
    ids = {name: user.id for name, user in people.items()}
    with tenant_scope(tenant_ctx.id):
        materials = _by_title(await materials_of(session, connector))
        assert "ENG-3: Оценка" not in materials
        for material in materials.values():
            assert material.visibility == MaterialVisibility.RESTRICTED.value
        vacation = materials["HR-1: Отпуск"]
        assert vacation.content.startswith("# HR-1: Отпуск")
        assert "Бюджет" not in vacation.content
        assert await access_of(session, vacation) == {ids["alice"], ids["carol"]}
        assert await access_of(session, materials["Заявление.md"]) == {
            ids["alice"],
            ids["carol"],
        }
        assert await access_of(session, materials["HR-2: Справка для Дэйва"]) == {
            ids["alice"],
            ids["carol"],
            ids["dave"],
        }
        incident = materials["ENG-2: Инцидент"]
        # bob в роли проекта, но не в уровне безопасности — не видит.
        assert await access_of(session, incident) == {ids["carol"]}
        # «Любой» — все активные; erin в Jira деактивирована.
        assert await access_of(session, materials["OPEN-1: Регламент"]) == {
            ids["alice"],
            ids["bob"],
            ids["carol"],
            ids["dave"],
        }


async def test_revoked_access_and_deletion_follow_jira(
    session: AsyncSession,
    tenant_ctx: Tenant,
    people: dict[str, User],
    secrets: SecretBox,
    server: FakeJira,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, server)
    await run(service, connector)

    # bob вышел из группы, задачу удалили, описание другой поменяли.
    server.groups["engineering"].discard("bob")
    server.issues.pop(server.issue_by_key("HR-2")["id"])
    regulation = server.issue_by_key("OPEN-1")
    regulation["description"] = "<p>С 9 до 18.</p>"
    regulation["updated"] = "2026-10-10T10:00:00.000+0000"

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED
    assert outcome.stats.removed == 1
    assert outcome.stats.updated == 1
    with tenant_scope(tenant_ctx.id):
        materials = _by_title(await materials_of(session, connector))
        assert "HR-2: Справка для Дэйва" not in materials
        assert "С 9 до 18." in materials["OPEN-1: Регламент"].content
        architecture = materials["ENG-1: Архитектура"]
        assert people["bob"].id not in await access_of(session, architecture)
        assert people["carol"].id in await access_of(session, architecture)


async def test_token_without_admin_rights_stops_the_connector(
    session: AsyncSession,
    tenant_ctx: Tenant,
    secrets: SecretBox,
    server: FakeJira,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, server, token=MEMBER_TOKEN)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.FAILED
    assert outcome.error_code == "admin_required"
    with tenant_scope(tenant_ctx.id):
        assert await materials_of(session, connector) == {}
        reloaded = await reload(session, connector)
        assert reloaded.status == ConnectorStatus.ERROR.value
