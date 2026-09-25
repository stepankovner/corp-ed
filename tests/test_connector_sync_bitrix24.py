"""Синхронизация с настоящим адаптером Битрикс24 против поддельного портала:
режим per_user, права, ссылки, обновление токенов, остановка по ошибке
приложения без отзыва грантов."""

from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import undefer

from corp_ed.connectors.registry import default_registry
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.secrets import SecretBox
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import (
    AuditEvent,
    Connector,
    ConnectorUserGrant,
    Tenant,
    User,
)
from corp_ed.domain.types import (
    ConnectorMode,
    ConnectorStatus,
    GrantStatus,
    MaterialVisibility,
    SyncRunStatus,
    SyncTrigger,
)
from corp_ed.repositories.audit_repository import AuditAction
from corp_ed.services.connector_sync_service import ConnectorSyncService, SyncOutcome
from tests.connectors.fake_portal import (
    ACCESS_TOKEN,
    CLIENT_ID,
    CLIENT_SECRET,
    EMPLOYEE_ID,
    OAUTH_SERVER,
    REFRESH_TOKEN,
    FakePortal,
    sample_portal,
)
from tests.fake_connector import plain_extractor
from tests.test_connector_sync import access_of, materials_of, reload

KEY = Fernet.generate_key().decode()


@pytest.fixture
def secrets() -> SecretBox:
    return SecretBox([KEY])


@pytest.fixture
def portal() -> FakePortal:
    return sample_portal()


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("corp_ed.connectors.bitrix24.adapter.MIN_INTERVAL", 0.0)


def make_settings(**overrides: Any) -> ConnectorSettings:
    return ConnectorSettings(
        secrets_keys=KEY, bitrix24_oauth_server=OAUTH_SERVER, **overrides
    )  # type: ignore[arg-type]


@pytest.fixture
def service(
    session_maker: async_sessionmaker[AsyncSession],
    portal: FakePortal,
    secrets: SecretBox,
) -> ConnectorSyncService:
    settings = make_settings()
    return ConnectorSyncService(
        session_maker,
        portal.client(),
        default_registry(settings),
        secrets,
        settings,
        extractor=plain_extractor,
    )


async def make_connector(
    session: AsyncSession,
    secrets: SecretBox,
    portal: FakePortal,
    *,
    client_secret: str = CLIENT_SECRET,
    modules: list[str] | None = None,
) -> Connector:
    connector = Connector(
        kind="bitrix24",
        name="Битрикс24",
        mode=ConnectorMode.PER_USER.value,
        modules=modules or ["disk", "knowledge_base"],
        config={"portal": portal.portal, "client_id": CLIENT_ID},
        credentials=secrets.encrypt({"client_secret": client_secret}),
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
    *,
    access_token: str = ACCESS_TOKEN,
    refresh_token: str = REFRESH_TOKEN,
) -> ConnectorUserGrant:
    grant = ConnectorUserGrant(
        connector_id=connector.id,
        user_id=user.id,
        credentials=secrets.encrypt(
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": "0",
            }
        ),
        external_user_id=EMPLOYEE_ID,
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


async def test_employee_listing_becomes_restricted_materials_with_links(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    admin: User,
    secrets: SecretBox,
    portal: FakePortal,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets, portal)
    await make_grant(session, secrets, connector, employee)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.PARTIAL  # пустая страница базы знаний
    assert outcome.stats.grants == 1
    assert outcome.stats.added == 4
    assert outcome.stats.failed == 1
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        assert set(materials) == {
            "disk:102",
            "disk:201",
            "kb:KNOWLEDGE:985",
            "kb:KNOWLEDGE:573",
        }
        vacation = materials["disk:102"]
        assert vacation.title == "Отпуск.txt"
        assert vacation.content == "Отпуск — 28 дней."
        assert vacation.source_url == f"{portal.portal}disk/file/Отпуск.txt"
        assert vacation.source_format == "txt"
        assert vacation.visibility == MaterialVisibility.RESTRICTED.value
        assert await access_of(session, vacation) == {employee.id}
        page = materials["kb:KNOWLEDGE:985"]
        assert page.source_format == "html"
        assert page.source_url == f"{portal.portal}knowledge/company/vacation/"
        assert "28 календарных дней" in page.content
        assert "Черновик" not in page.content
        assert await access_of(session, page) == {employee.id}
        reloaded = await reload(session, connector)
        assert reloaded.status == ConnectorStatus.ACTIVE.value


async def test_refreshed_tokens_are_saved_into_grant(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    secrets: SecretBox,
    portal: FakePortal,
    service: ConnectorSyncService,
) -> None:
    portal.expired.add(ACCESS_TOKEN)
    connector = await make_connector(session, secrets, portal, modules=["disk"])
    grant = await make_grant(session, secrets, connector, employee)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED
    with tenant_scope(tenant_ctx.id):
        saved = secrets.decrypt((await grant_of(session, grant)).credentials)
    assert saved["access_token"] != ACCESS_TOKEN
    assert saved["refresh_token"] != REFRESH_TOKEN
    assert saved["access_token"] in portal.access_tokens
    # Второй запуск идёт уже с новыми токенами, без обмена.
    calls_before = len(portal.calls)
    outcome = await run(service, connector)
    assert outcome.status is SyncRunStatus.SUCCEEDED
    assert "oauth/token" not in [m for m, _ in portal.calls[calls_before:]]


async def test_rejected_app_secret_stops_connector_and_keeps_grants(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    secrets: SecretBox,
    portal: FakePortal,
    service: ConnectorSyncService,
) -> None:
    portal.expired.add(ACCESS_TOKEN)
    connector = await make_connector(
        session, secrets, portal, client_secret="wrong", modules=["disk"]
    )
    grant = await make_grant(session, secrets, connector, employee)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.FAILED
    assert outcome.error_code == "invalid_client"
    assert not outcome.retryable
    with tenant_scope(tenant_ctx.id):
        reloaded = await reload(session, connector)
        assert reloaded.status == ConnectorStatus.ERROR.value
        assert reloaded.last_error_code == "invalid_client"
        assert (await grant_of(session, grant)).status == GrantStatus.ACTIVE.value
        events = await session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == AuditAction.CONNECTOR_STOPPED.value
            )
        )
        assert [e.details["code"] for e in events] == ["invalid_client"]


async def test_dead_employee_token_expires_only_that_grant(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    secrets: SecretBox,
    portal: FakePortal,
    service: ConnectorSyncService,
) -> None:
    portal.access_tokens.clear()
    portal.refresh_tokens.clear()
    connector = await make_connector(session, secrets, portal, modules=["disk"])
    grant = await make_grant(session, secrets, connector, employee)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED
    assert outcome.stats.grants_expired == 1
    with tenant_scope(tenant_ctx.id):
        saved = await grant_of(session, grant)
        assert saved.status == GrantStatus.EXPIRED.value
        assert saved.error_code == "invalid_token"
        assert (await reload(session, connector)).status == ConnectorStatus.ACTIVE.value


async def test_knowledge_base_v2_documents_are_stored_as_markdown(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    secrets: SecretBox,
    portal: FakePortal,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(
        session, secrets, portal, modules=["knowledge_base_v2"]
    )
    await make_grant(session, secrets, connector, employee)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.PARTIAL  # пустая заметка
    assert outcome.stats.added == 3
    assert outcome.stats.failed == 1
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        assert set(materials) == {"note:10", "note:11", "note:20"}
        chapter = materials["note:11"]
        assert chapter.source_format == "md"
        assert chapter.content == "# Глава 1\n\nПервые шаги."
        assert chapter.external_version == "2026-05-01T10:00:00Z"
        assert chapter.source_url == f"{portal.portal}knowledge/"
        assert await access_of(session, chapter) == {employee.id}
    # Повторный запуск: версии не изменились — документы не перечитываются.
    calls_before = len(portal.calls)
    outcome = await run(service, connector)
    assert outcome.stats.updated == 0
    gets = [m for m, _ in portal.calls[calls_before:] if m == "note.document.get"]
    # Версия известна только из note.document.get — он вызывается при обходе.
    assert len(gets) == 4


async def test_missing_scope_stops_connector_as_config_error(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    secrets: SecretBox,
    portal: FakePortal,
    service: ConnectorSyncService,
) -> None:
    portal.canned["landing.site.getlist"] = (401, {"error": "insufficient_scope"})
    connector = await make_connector(session, secrets, portal)
    grant = await make_grant(session, secrets, connector, employee)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.FAILED
    assert outcome.error_code == "insufficient_scope"
    with tenant_scope(tenant_ctx.id):
        assert (await reload(session, connector)).status == ConnectorStatus.ERROR.value
        assert (await grant_of(session, grant)).status == GrantStatus.ACTIVE.value
        # Диск успел синхронизироваться до ошибки: сделанное не откатывается.
        assert "disk:102" in await materials_of(session, connector)
