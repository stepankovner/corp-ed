"""Режим per_user: документы, которые владелец открыл коллеге лично.

Яндекс Диск не кладёт такие папки в листинг получателя («Общий доступ»),
а назвать их может только администратор организации. Ядро спрашивает
источник токеном того, кому он отвечает, и открывает получателю
документы из листинга владельца под открытой папкой."""

from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.base import SharedResource
from corp_ed.core.outbound import OutboundClient
from corp_ed.core.secrets import SecretBox
from corp_ed.domain.models import Connector, ConnectorUserGrant, Tenant, User, UserRole
from corp_ed.domain.types import GrantStatus, SyncRunStatus
from corp_ed.services.connector_sync_service import ConnectorSyncService
from tests.factories import make_user
from tests.fake_connector import FAKE_PER_USER_KIND, FakeSource, doc
from tests.test_connector_sync import (
    access_of,
    make_connector,
    make_service,
    materials_of,
    run,
)

SHARED = SharedResource(owner="uid-owner", locator="disk:/Общие")
KEY = Fernet.generate_key().decode()


@pytest.fixture
def secrets() -> SecretBox:
    return SecretBox([KEY])


@pytest.fixture
def source() -> FakeSource:
    return FakeSource()


@pytest.fixture
def service(
    session_maker: async_sessionmaker[AsyncSession],
    source: FakeSource,
    secrets: SecretBox,
) -> ConnectorSyncService:
    http = OutboundClient(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    )
    return make_service(session_maker, source, secrets, http)


@pytest.fixture
async def owner(session: AsyncSession, tenant_ctx: Tenant) -> User:
    user = make_user(
        id=uuid4(),
        tenant_id=tenant_ctx.id,
        email="owner@test.com",
        role=UserRole.EMPLOYEE,
        hashed_password="hashed",
    )
    session.add(user)
    await session.commit()
    return user


async def grant(
    session: AsyncSession,
    secrets: SecretBox,
    connector: Connector,
    user: User,
    token: str,
    external: str,
) -> ConnectorUserGrant:
    row = ConnectorUserGrant(
        connector_id=connector.id,
        user_id=user.id,
        credentials=secrets.encrypt({"token": token}),
        external_user_id=external,
    )
    session.add(row)
    await session.commit()
    return row


async def grants_of(
    session: AsyncSession, connector: Connector
) -> list[ConnectorUserGrant]:
    result = await session.scalars(
        select(ConnectorUserGrant)
        .where(ConnectorUserGrant.connector_id == connector.id)
        .execution_options(populate_existing=True)
    )
    return list(result)


@pytest.fixture
def owner_disk(source: FakeSource) -> FakeSource:
    source.add(doc("shared", locator="disk:/Общие/План.txt"), "План.")
    source.add(doc("deep", locator="disk:/Общие/Отдел/Приказ.txt"), "Приказ.")
    source.add(doc("lookalike", locator="disk:/Общие 2/Черновик.txt"), "Черновик.")
    source.add(doc("private", locator="disk:/Личное/Заметка.txt"), "Заметка.")
    source.visible_to = {
        "t-owner": {"shared", "deep", "lookalike", "private"},
        "t-emp": set(),
        "t-admin": set(),
    }
    source.share_admins = {"t-admin"}
    source.shares = {"uid-emp": [SHARED]}
    return source


async def setup(
    session: AsyncSession,
    secrets: SecretBox,
    admin: User,
    employee: User,
    owner: User,
) -> Connector:
    connector = await make_connector(session, secrets, kind=FAKE_PER_USER_KIND)
    # Получатель подключился раньше владельца: обходится первым.
    await grant(session, secrets, connector, employee, "t-emp", "uid-emp")
    await grant(session, secrets, connector, owner, "t-owner", "uid-owner")
    await grant(session, secrets, connector, admin, "t-admin", "uid-admin")
    return connector


async def test_recipient_sees_documents_under_the_shared_folder_only(
    session: AsyncSession,
    tenant_ctx: Tenant,
    admin: User,
    employee: User,
    owner: User,
    secrets: SecretBox,
    owner_disk: FakeSource,
    service: ConnectorSyncService,
) -> None:
    connector = await setup(session, secrets, admin, employee, owner)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED
    assert outcome.stats.shared == 2
    found = await materials_of(session, connector)
    assert await access_of(session, found["shared"]) == {owner.id, employee.id}
    assert await access_of(session, found["deep"]) == {owner.id, employee.id}
    assert await access_of(session, found["lookalike"]) == {owner.id}
    assert await access_of(session, found["private"]) == {owner.id}
    # Сначала спросили не администратора: отказ, а грант жив.
    assert owner_disk.share_calls[0][0] == "t-emp"
    assert all(
        g.status == GrantStatus.ACTIVE.value
        for g in await grants_of(session, connector)
    )


async def test_access_is_stable_and_follows_the_share(
    session: AsyncSession,
    tenant_ctx: Tenant,
    admin: User,
    employee: User,
    owner: User,
    secrets: SecretBox,
    owner_disk: FakeSource,
    service: ConnectorSyncService,
) -> None:
    connector = await setup(session, secrets, admin, employee, owner)
    await run(service, connector)

    # Второй запуск ничего не отбирает: получатель обходится раньше
    # владельца, но права снимаются после обхода всех.
    await run(service, connector)
    found = await materials_of(session, connector)
    assert await access_of(session, found["shared"]) == {owner.id, employee.id}

    # Владелец закрыл папку — доступ пропадает со следующим запуском.
    owner_disk.shares = {"uid-emp": []}
    await run(service, connector)
    assert await access_of(session, found["shared"]) == {owner.id}
    assert await access_of(session, found["deep"]) == {owner.id}


async def test_single_shared_file(
    session: AsyncSession,
    tenant_ctx: Tenant,
    admin: User,
    employee: User,
    owner: User,
    secrets: SecretBox,
    owner_disk: FakeSource,
    service: ConnectorSyncService,
) -> None:
    owner_disk.shares = {
        "uid-emp": [SharedResource("uid-owner", "disk:/Личное/Заметка.txt")]
    }
    connector = await setup(session, secrets, admin, employee, owner)

    await run(service, connector)

    found = await materials_of(session, connector)
    assert await access_of(session, found["private"]) == {owner.id, employee.id}
    assert await access_of(session, found["shared"]) == {owner.id}


async def test_without_an_admin_nothing_is_shared_and_old_access_goes(
    session: AsyncSession,
    tenant_ctx: Tenant,
    admin: User,
    employee: User,
    owner: User,
    secrets: SecretBox,
    owner_disk: FakeSource,
    service: ConnectorSyncService,
) -> None:
    connector = await setup(session, secrets, admin, employee, owner)
    await run(service, connector)

    # Администратор отключился: проверить права некому — не показываем.
    owner_disk.share_admins = set()
    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED
    assert outcome.stats.shared == 0
    found = await materials_of(session, connector)
    assert await access_of(session, found["shared"]) == {owner.id}
    assert all(
        g.status == GrantStatus.ACTIVE.value
        for g in await grants_of(session, connector)
    )


async def test_unavailable_source_keeps_access_and_retries(
    session: AsyncSession,
    tenant_ctx: Tenant,
    admin: User,
    employee: User,
    owner: User,
    secrets: SecretBox,
    owner_disk: FakeSource,
    service: ConnectorSyncService,
) -> None:
    connector = await setup(session, secrets, admin, employee, owner)
    await run(service, connector)

    owner_disk.shares_unavailable = True
    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.FAILED and outcome.retryable
    found = await materials_of(session, connector)
    assert await access_of(session, found["shared"]) == {owner.id, employee.id}


async def test_owner_outside_kronto_shares_nothing(
    session: AsyncSession,
    tenant_ctx: Tenant,
    admin: User,
    employee: User,
    owner: User,
    secrets: SecretBox,
    owner_disk: FakeSource,
    service: ConnectorSyncService,
) -> None:
    owner_disk.shares = {"uid-emp": [SharedResource("uid-stranger", "disk:/Общие")]}
    connector = await setup(session, secrets, admin, employee, owner)

    outcome = await run(service, connector)

    assert outcome.stats.shared == 0
    found = await materials_of(session, connector)
    assert await access_of(session, found["shared"]) == {owner.id}


async def test_source_without_shares_is_not_asked_twice(
    session: AsyncSession,
    tenant_ctx: Tenant,
    admin: User,
    employee: User,
    owner: User,
    secrets: SecretBox,
    source: FakeSource,
    service: ConnectorSyncService,
) -> None:
    source.add(doc("d1", locator="disk:/a.txt"), "Один.")
    connector = await setup(session, secrets, admin, employee, owner)

    outcome = await run(service, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED
    assert len(source.share_calls) == 1  # None — источник этого не умеет
    found = await materials_of(session, connector)
    assert await access_of(session, found["d1"]) == {admin.id, employee.id, owner.id}
