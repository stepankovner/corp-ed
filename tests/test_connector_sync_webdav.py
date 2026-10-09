"""Синхронизация WebDAV-дисков (режим per_user) против поддельного сервера:
общая папка Nextcloud — один материал с доступом обоим, свои файлы —
только владельцу; правка, удаление и отзыв шары доходят до материалов;
без сквозного fileid одинаковые пути двух сотрудников не склеиваются."""

from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.fernet import Fernet
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
from corp_ed.repositories.connector_repository import GrantRepository
from corp_ed.services.connector_sync_service import ConnectorSyncService, SyncOutcome
from tests.connectors.fake_webdav import (
    HOST,
    IVAN,
    IVAN_LOGIN,
    IVAN_PASSWORD,
    MARIA,
    MARIA_LOGIN,
    MARIA_PASSWORD,
    SERVER,
    FakeDav,
    sample_nextcloud,
)
from tests.fake_connector import plain_extractor
from tests.test_connector_sync import access_of, materials_of

KEY = Fernet.generate_key().decode()


@pytest.fixture
def secrets() -> SecretBox:
    return SecretBox([KEY])


def make_service(
    session_maker: async_sessionmaker[AsyncSession],
    server: FakeDav,
    secrets: SecretBox,
) -> ConnectorSyncService:
    settings = ConnectorSettings(secrets_keys=KEY)  # type: ignore[arg-type]
    return ConnectorSyncService(
        session_maker,
        server.client(),
        default_registry(settings),
        secrets,
        settings,
        extractor=plain_extractor,
    )


async def make_connector(
    session: AsyncSession, kind: str, config: dict[str, Any]
) -> Connector:
    connector = Connector(
        kind=kind,
        name="Файлы",
        mode=ConnectorMode.PER_USER.value,
        modules=["files"],
        config=config,
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
    login: str,
    password: str,
) -> ConnectorUserGrant:
    grant = ConnectorUserGrant(
        connector_id=connector.id,
        user_id=user.id,
        credentials=secrets.encrypt({"login": login, "password": password}),
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


def by_title(materials: dict[str, Any], title: str, content: str = "") -> Any:
    found = [m for m in materials.values() if m.title == title and content in m.content]
    assert len(found) == 1, [m.title for m in materials.values()]
    return found[0]


async def test_nextcloud_shared_folder_is_one_material_for_both(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    admin: User,
    employee: User,
    secrets: SecretBox,
) -> None:
    server = sample_nextcloud()
    service = make_service(session_maker, server, secrets)
    connector = await make_connector(session, "nextcloud", {"server": SERVER})
    await make_grant(session, secrets, connector, admin, IVAN_LOGIN, IVAN_PASSWORD)
    await make_grant(session, secrets, connector, employee, MARIA_LOGIN, MARIA_PASSWORD)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED, outcome
    assert outcome.stats.grants == 2
    # У Ивана четыре, у Марии свой «Отпуск» и два из общей папки Ивана.
    assert outcome.stats.added == 5
    assert outcome.stats.skipped_formats == {".png": 1}
    assert outcome.stats.too_large == 1
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        plan = by_title(materials, "План 100%.md")
        assert plan.visibility == MaterialVisibility.RESTRICTED.value
        assert await access_of(session, plan) == {admin.id, employee.id}
        ivan_leave = by_title(materials, "Отпуск.txt", "28 дней")
        maria_leave = by_title(materials, "Отпуск.txt", "июле")
        assert await access_of(session, ivan_leave) == {admin.id}
        assert await access_of(session, maria_leave) == {employee.id}
        note = by_title(materials, "Заметка.txt")
        assert await access_of(session, note) == {admin.id}
        assert note.source_url.startswith(f"{SERVER}index.php/f/")

    # Иван правит заметку, удаляет архив и закрывает Марии общую папку.
    server.change(IVAN, "Заметка.txt", "Заметка Ивана, версия 2.".encode())
    server.remove(IVAN, "Проекты/Архив")
    server.remove(MARIA, "Общее от Ивана")
    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED, outcome
    assert outcome.stats.updated == 1
    assert outcome.stats.removed == 1
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        assert by_title(materials, "Заметка.txt").content.endswith("версия 2.")
        assert not [m for m in materials.values() if m.title == "Старый.md"]
        plan = by_title(materials, "План 100%.md")
        assert await access_of(session, plan) == {admin.id}


async def test_plain_webdav_same_paths_stay_separate(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    admin: User,
    employee: User,
    secrets: SecretBox,
) -> None:
    server = sample_nextcloud("plain")
    service = make_service(session_maker, server, secrets)
    connector = await make_connector(
        session, "webdav", {"server": f"https://{HOST}/dav/", "folders": "Документы"}
    )
    await make_grant(session, secrets, connector, admin, IVAN_LOGIN, IVAN_PASSWORD)
    await make_grant(session, secrets, connector, employee, MARIA_LOGIN, MARIA_PASSWORD)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED, outcome
    assert outcome.stats.added == 2
    with tenant_scope(tenant_ctx.id):
        materials = await materials_of(session, connector)
        ivan_leave = by_title(materials, "Отпуск.txt", "28 дней")
        maria_leave = by_title(materials, "Отпуск.txt", "июле")
        assert await access_of(session, ivan_leave) == {admin.id}
        assert await access_of(session, maria_leave) == {employee.id}


async def test_wrong_app_password_expires_only_that_grant(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    admin: User,
    employee: User,
    secrets: SecretBox,
) -> None:
    server = sample_nextcloud()
    service = make_service(session_maker, server, secrets)
    connector = await make_connector(session, "nextcloud", {"server": SERVER})
    await make_grant(session, secrets, connector, admin, IVAN_LOGIN, IVAN_PASSWORD)
    bad = await make_grant(
        session, secrets, connector, employee, MARIA_LOGIN, "revoked-app-password"
    )

    outcome = await run(service, connector)

    assert outcome.stats.grants_expired == 1
    assert outcome.stats.added == 4
    with tenant_scope(tenant_ctx.id):
        grant = await GrantRepository(session).get_by_id(bad.id)
        assert grant is not None
        await session.refresh(grant)
        assert grant.status == GrantStatus.EXPIRED.value
        assert grant.error_code == "auth_failed"


async def test_tree_too_large_fails_the_run_without_deleting(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    admin: User,
    secrets: SecretBox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = sample_nextcloud()
    service = make_service(session_maker, server, secrets)
    connector = await make_connector(session, "nextcloud", {"server": SERVER})
    await make_grant(session, secrets, connector, admin, IVAN_LOGIN, IVAN_PASSWORD)
    assert (await run(service, connector)).stats.added == 4

    monkeypatch.setattr("corp_ed.connectors.webdav.adapter.MAX_ENTRIES", 3)
    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.FAILED
    # Код — тот, что объясняет администратору, что делать (сузить папки).
    assert outcome.error_code == "tree_too_large"
    assert not outcome.retryable
    assert outcome.stats.removed == 0
    with tenant_scope(tenant_ctx.id):
        assert len(await materials_of(session, connector)) == 4
