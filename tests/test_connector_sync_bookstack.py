"""Синхронизация с адаптером BookStack (режим organization) против
поддельного сервера: «вся компания» при общей роли с правом у каждого,
почты тех, кто видит, для закрытого, изменения прав и удаление в
источнике. Вид ещё не в default_registry — реестр свой."""

from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.bookstack import register
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
from tests.connectors.fake_bookstack import (
    ADMIN_TOKEN,
    EDITOR_TOKEN,
    HR_ROLE,
    STAFF_ROLE,
    FakeBookStack,
    sample_bookstack,
)
from tests.factories import make_user
from tests.fake_connector import plain_extractor
from tests.test_connector_sync import access_of, materials_of, reload

KEY = Fernet.generate_key().decode()


@pytest.fixture
def secrets() -> SecretBox:
    return SecretBox([KEY])


@pytest.fixture
def server() -> FakeBookStack:
    return sample_bookstack()


@pytest.fixture
def service(
    session_maker: async_sessionmaker[AsyncSession],
    server: FakeBookStack,
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
async def people(session: AsyncSession, tenant_ctx: Tenant) -> dict[str, User]:
    return {
        login: await _user(session, f"{login}@example.com")
        for login in ("ivan", "maria", "petr", "olga")
    }


async def make_connector(
    session: AsyncSession,
    secrets: SecretBox,
    server: FakeBookStack,
    *,
    token: tuple[str, str] = ADMIN_TOKEN,
    modules: tuple[str, ...] = ("pages", "attachments"),
) -> Connector:
    connector = Connector(
        kind="bookstack",
        name="BookStack",
        mode=ConnectorMode.ORGANIZATION.value,
        modules=list(modules),
        config={"base_url": server.base},
        credentials=secrets.encrypt({"token_id": token[0], "token_secret": token[1]}),
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


def _page(server: FakeBookStack, name: str) -> int:
    return next(p["id"] for p in server.pages.values() if p["name"] == name)


async def test_pages_become_company_or_reader_materials(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    people: dict[str, User],
    secrets: SecretBox,
    server: FakeBookStack,
    service: ConnectorSyncService,
) -> None:
    server.add_attachment(
        _page(server, "Зарплаты"), "ведомость.txt", "Ведомость.".encode()
    )
    connector = await make_connector(session, secrets, server)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED
    assert outcome.stats.added == 10
    with tenant_scope(tenant_ctx.id):
        materials = {
            m.title: m for m in (await materials_of(session, connector)).values()
        }
        holiday = materials["Отпуск"]
        assert holiday.visibility == MaterialVisibility.TENANT.value
        assert "Отпуск — 28 дней." in holiday.content
        salaries = materials["Зарплаты"]
        assert salaries.visibility == MaterialVisibility.RESTRICTED.value
        # Сотрудник kronto без доступа в BookStack документ не видит.
        assert await access_of(session, salaries) == {people["maria"].id}
        assert await access_of(session, materials["ведомость.txt"]) == {
            people["maria"].id
        }
        # Почта в BookStack — «Ivan@Example.com»: сопоставляется без регистра.
        assert await access_of(session, materials["План"]) == {people["ivan"].id}
        assert await access_of(session, materials["Заметки"]) == {
            people["ivan"].id,
            people["olga"].id,
        }
        # Только Admin (его в kronto нет) — не видит никто из сотрудников.
        assert await access_of(session, materials["Смета"]) == set()


async def test_rights_changes_and_deletion_follow_the_source(
    session: AsyncSession,
    tenant_ctx: Tenant,
    people: dict[str, User],
    secrets: SecretBox,
    server: FakeBookStack,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, server, modules=("pages",))
    await run(service, connector)

    plan = _page(server, "План")
    server.pages[plan]["revision_count"] = 2
    server.pages[plan]["updated_at"] = "2026-10-01T10:00:00.000000Z"
    server.html[plan] = "<p>Запуск перенесён на июнь.</p>"
    # Удаление в корзину: API страницу больше не отдаёт.
    del server.pages[_page(server, "Смета")]
    # maria вышла из «Кадров»; книгу «Кадры» открыли «Сотрудникам» —
    # права меняются без новой версии документа.
    server.role_users[HR_ROLE].remove(4)
    hr = next(b["id"] for b in server.books.values() if b["name"] == "Кадры")
    server.grant("book", hr, {HR_ROLE: True, STAFF_ROLE: True}, fallback=False)

    outcome = await run(service, connector)

    assert outcome.stats.updated == 1
    assert outcome.stats.removed == 1
    with tenant_scope(tenant_ctx.id):
        materials = {
            m.title: m for m in (await materials_of(session, connector)).values()
        }
        assert "Смета" not in materials
        assert "июнь" in materials["План"].content
        # Книга открыта всем «Сотрудникам» (общая роль): вся компания.
        assert materials["Зарплаты"].visibility == MaterialVisibility.TENANT.value
        # «Премии» — только «Кадры», где maria больше нет.
        assert await access_of(session, materials["Премии"]) == set()


async def test_token_without_user_management_stops_the_connector(
    session: AsyncSession,
    tenant_ctx: Tenant,
    secrets: SecretBox,
    server: FakeBookStack,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, server, token=EDITOR_TOKEN)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.FAILED
    assert outcome.error_code == "admin_required"
    with tenant_scope(tenant_ctx.id):
        reloaded = await reload(session, connector)
        assert reloaded.status == ConnectorStatus.ERROR.value
        assert await materials_of(session, connector) == {}
