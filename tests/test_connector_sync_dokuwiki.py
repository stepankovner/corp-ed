"""Синхронизация с адаптером DokuWiki (режим organization) против
поддельного сервера: права из core.aclCheck для сотрудников из списка,
сотрудник без доступа документа не видит, изменения, удаление и смена
прав в источнике."""

from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.dokuwiki import register
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
from tests.connectors.fake_dokuwiki import (
    ADMIN_TOKEN,
    MEMBER_TOKEN,
    FakeDokuWiki,
    sample_dokuwiki,
)
from tests.factories import make_user
from tests.fake_connector import plain_extractor
from tests.test_connector_sync import access_of, materials_of, reload

KEY = Fernet.generate_key().decode()
USERS = "\n".join(
    f"{login} {login}@example.com" for login in ("admin", "ivan", "maria", "petr")
)


@pytest.fixture
def secrets() -> SecretBox:
    return SecretBox([KEY])


@pytest.fixture
def server() -> FakeDokuWiki:
    return sample_dokuwiki()


@pytest.fixture
def service(
    session_maker: async_sessionmaker[AsyncSession],
    server: FakeDokuWiki,
    secrets: SecretBox,
) -> ConnectorSyncService:
    settings: Any = ConnectorSettings(secrets_keys=KEY)  # type: ignore[arg-type]
    # Свой реестр: подключение вида к default_registry — дело интегратора.
    registry = AdapterRegistry(enabled_preview_kinds=frozenset({"dokuwiki"}))
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
    """Сотрудники kronto; olga есть в DokuWiki, но не в списке подключения."""
    return {
        login: await _user(session, f"{login}@example.com")
        for login in ("ivan", "maria", "petr", "olga")
    }


async def make_connector(
    session: AsyncSession,
    secrets: SecretBox,
    server: FakeDokuWiki,
    *,
    token: str = ADMIN_TOKEN,
    modules: tuple[str, ...] = ("pages", "media"),
) -> Connector:
    connector = Connector(
        kind="dokuwiki",
        name="DokuWiki",
        mode=ConnectorMode.ORGANIZATION.value,
        modules=list(modules),
        config={"base_url": server.base, "users": USERS},
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


def _ids(people: dict[str, User], *logins: str) -> set[Any]:
    return {people[login].id for login in logins}


async def test_pages_and_media_follow_dokuwiki_acl(
    session: AsyncSession,
    tenant_ctx: Tenant,
    people: dict[str, User],
    secrets: SecretBox,
    server: FakeDokuWiki,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, server)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED, outcome
    assert outcome.stats.added == 8
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        assert set(materials) == {
            "page:start",
            "page:public:vacation",
            "page:public:fresh",
            "page:hr:onboarding",
            "page:hr:salaries",
            "page:sales:plan",
            "media:hr:policy.pdf",
            "media:public:guide.pdf",
        }
        for material in materials.values():
            assert material.visibility == MaterialVisibility.RESTRICTED.value
        vacation = materials["page:public:vacation"]
        assert "Отпуск — 28 дней." in vacation.content
        # olga нет в списке «логин почта»: не видит даже открытое всем.
        assert await access_of(session, vacation) == _ids(
            people, "ivan", "maria", "petr"
        )
        assert await access_of(session, materials["page:hr:onboarding"]) == _ids(
            people, "maria", "petr"
        )
        # petr в группе hr, но страница ему запрещена отдельно.
        assert await access_of(session, materials["page:hr:salaries"]) == _ids(
            people, "maria"
        )
        assert await access_of(session, materials["page:sales:plan"]) == _ids(
            people, "ivan"
        )
        assert await access_of(session, materials["media:hr:policy.pdf"]) == _ids(
            people, "maria", "petr"
        )


async def test_changes_rights_and_deletion_follow_the_source(
    session: AsyncSession,
    tenant_ctx: Tenant,
    people: dict[str, User],
    secrets: SecretBox,
    server: FakeDokuWiki,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, server)
    await run(service, connector)

    server.pages["public:vacation"]["html"] = "<h1>Отпуск</h1><p>Теперь 31 день.</p>"
    server.pages["public:vacation"]["revision"] = 1_770_000_000
    del server.pages["sales:plan"]
    del server.media["hr:policy.pdf"]
    # Запрет для petr сняли, maria вышла из группы hr: права меняются без
    # новой ревизии страницы, группы DokuWiki берёт из своего бэкенда.
    server.acl.remove(("hr:salaries", "petr", 0))
    server.users["maria"]["grps"].remove("hr")

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED, outcome
    assert outcome.stats.updated == 1
    assert outcome.stats.removed == 2
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        assert "page:sales:plan" not in materials
        assert "media:hr:policy.pdf" not in materials
        vacation = materials["page:public:vacation"]
        assert "Теперь 31 день." in vacation.content
        assert await access_of(session, vacation) == _ids(
            people, "ivan", "maria", "petr"
        )
        assert await access_of(session, materials["page:hr:salaries"]) == _ids(
            people, "petr"
        )
        assert await access_of(session, materials["page:hr:onboarding"]) == _ids(
            people, "petr"
        )


async def test_member_token_stops_the_connector(
    session: AsyncSession,
    tenant_ctx: Tenant,
    secrets: SecretBox,
    server: FakeDokuWiki,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, server, token=MEMBER_TOKEN)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.FAILED
    assert outcome.error_code == "admin_required"
    with tenant_scope(tenant_ctx.id):
        reloaded = await reload(session, connector)
        assert reloaded.status == ConnectorStatus.ERROR.value
