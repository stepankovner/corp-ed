"""Синхронизация с адаптером Яндекс Диска (режим per_user) против
поддельных Яндекс ID и Диска: материалы сотрудника, обновлённые токены."""

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
from corp_ed.domain.models import Connector, ConnectorUserGrant, Tenant, User
from corp_ed.domain.types import (
    ConnectorMode,
    MaterialVisibility,
    SyncRunStatus,
    SyncTrigger,
)
from corp_ed.services.connector_sync_service import ConnectorSyncService, SyncOutcome
from tests.connectors.fake_yandex import (
    ACCESS_TOKEN,
    CLIENT_ID,
    CLIENT_SECRET,
    DISK_API,
    LOGIN,
    OAUTH_SERVER,
    ORG_ID,
    REFRESH_TOKEN,
    TRACKER_API,
    FakeYandex,
    sample_tracker,
    sample_yandex,
)
from tests.fake_connector import plain_extractor
from tests.test_connector_sync import access_of, materials_of

KEY = Fernet.generate_key().decode()


@pytest.fixture
def secrets() -> SecretBox:
    return SecretBox([KEY])


@pytest.fixture
def server() -> FakeYandex:
    return sample_yandex()


def make_settings(**overrides: Any) -> ConnectorSettings:
    return ConnectorSettings(
        secrets_keys=KEY,
        yandex_oauth_server=OAUTH_SERVER,
        yandex_disk_api=DISK_API,
        yandex_tracker_api=TRACKER_API,
        **overrides,
    )  # type: ignore[arg-type]


@pytest.fixture
def service(
    session_maker: async_sessionmaker[AsyncSession],
    server: FakeYandex,
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
    *,
    modules: list[str] | None = None,
    config: dict[str, str] | None = None,
) -> Connector:
    connector = Connector(
        kind="yandex360",
        name="Яндекс 360",
        mode=ConnectorMode.PER_USER.value,
        modules=modules or ["disk"],
        config={"client_id": CLIENT_ID, **(config or {})},
        credentials=secrets.encrypt({"client_secret": CLIENT_SECRET}),
        credentials_set_at=datetime.now(UTC),
    )
    session.add(connector)
    await session.commit()
    return connector


async def make_grant(
    session: AsyncSession, secrets: SecretBox, connector: Connector, user: User
) -> ConnectorUserGrant:
    grant = ConnectorUserGrant(
        connector_id=connector.id,
        user_id=user.id,
        credentials=secrets.encrypt(
            {
                "access_token": ACCESS_TOKEN,
                "refresh_token": REFRESH_TOKEN,
                "expires_at": "0",
            }
        ),
        external_user_id=LOGIN,
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


async def test_employee_disk_becomes_restricted_materials(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    secrets: SecretBox,
    server: FakeYandex,
    service: ConnectorSyncService,
) -> None:
    connector = await make_connector(session, secrets)
    await make_grant(session, secrets, connector, employee)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED
    assert outcome.stats.added == 4
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        assert set(materials) == {
            "ydisk:rid-disk:/Регламенты/Отпуск.txt",
            "ydisk:rid-disk:/Регламенты/Архив/Старый.md",
            "ydisk:rid-disk:/Общая папка/План.md",
            "ydisk:rid-disk:/Заметка.txt",
        }
        plan = materials["ydisk:rid-disk:/Общая папка/План.md"]
        assert plan.content == "# План\n\nСрок — май."
        assert plan.source_format == "md"
        assert plan.visibility == MaterialVisibility.RESTRICTED.value
        assert await access_of(session, plan) == {employee.id}
        assert plan.source_url.startswith("https://disk.yandex.ru/client/disk/")


async def test_refreshed_yandex_tokens_are_saved(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    secrets: SecretBox,
    server: FakeYandex,
    service: ConnectorSyncService,
) -> None:
    server.expired.add(ACCESS_TOKEN)
    connector = await make_connector(session, secrets)
    grant = await make_grant(session, secrets, connector, employee)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED
    with tenant_scope(tenant_ctx.id):
        saved = (
            await session.scalars(
                select(ConnectorUserGrant)
                .options(undefer(ConnectorUserGrant.credentials))
                .where(ConnectorUserGrant.id == grant.id)
                .execution_options(populate_existing=True)
            )
        ).one()
        credentials = secrets.decrypt(saved.credentials)
    assert credentials["access_token"] != ACCESS_TOKEN
    assert credentials["access_token"] in server.access_tokens
    # Яндекс выдаёт новый refresh при продлении; старый больше не действует.
    assert credentials["refresh_token"] != REFRESH_TOKEN
    assert credentials["refresh_token"] in server.refresh_tokens


async def test_tracker_issues_are_visible_to_the_employee_only(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    secrets: SecretBox,
    server: FakeYandex,
    service: ConnectorSyncService,
) -> None:
    sample_tracker(server)
    # «Огромный.docx» здесь не огромный (лимит синхронизации — 25 МиБ) и не
    # docx: в этом тесте он не нужен.
    issue = server.issues["id-1"]
    issue["_attachments"] = [a for a in issue["_attachments"] if a["id"] != "a3"]
    connector = await make_connector(
        session, secrets, modules=["tracker"], config={"org_id": ORG_ID}
    )
    await make_grant(session, secrets, connector, employee)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED, outcome.error_code
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        assert set(materials) == {
            "ytracker:id-1",
            "ytracker:id-2",
            "ytracker:id-4",
            "ytracker-file:id-1:a1",
        }
        issue = materials["ytracker:id-1"]
        assert issue.title == "SUP-1: Клиент Альфа: сроки доставки"
        assert issue.source_url == "https://tracker.yandex.ru/SUP-1"
        assert "Решили: доставка за 3 дня." in issue.content
        assert issue.visibility == MaterialVisibility.RESTRICTED.value
        assert await access_of(session, issue) == {employee.id}

    # Задачу удалили (или сотрудник потерял к ней доступ) — материал уходит.
    server.tracker_hidden.add("id-2")
    await run(service, connector)
    with tenant_scope(tenant_ctx.id):
        assert "ytracker:id-2" not in await materials_of(session, connector)
