"""Удаление подключения и отключение сотрудника: токены отзываются у
провайдера (Яндекс ID), но ошибка отзыва удалению не мешает и в журнал
попадают только тип ошибки и код — без токенов и тел ответов."""

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from corp_ed.connectors.registry import default_registry
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.outbound import OutboundClient
from corp_ed.core.secrets import SecretBox
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import Connector, ConnectorUserGrant, Tenant, User
from corp_ed.domain.types import ConnectorMode, GrantStatus
from corp_ed.repositories.audit_repository import AuditRepository
from corp_ed.repositories.connector_repository import (
    ConnectorRepository,
    GrantRepository,
    SyncRunRepository,
)
from corp_ed.repositories.connector_sync_job_repository import (
    ConnectorSyncJobRepository,
)
from corp_ed.repositories.material_repository import MaterialRepository
from corp_ed.services import connector_service
from corp_ed.services.connector_service import ConnectorService
from tests.connectors.fake_portal import (
    CLIENT_ID as BITRIX_CLIENT_ID,
)
from tests.connectors.fake_portal import (
    CLIENT_SECRET as BITRIX_CLIENT_SECRET,
)
from tests.connectors.fake_portal import (
    OAUTH_SERVER as BITRIX_OAUTH_SERVER,
)
from tests.connectors.fake_portal import sample_portal
from tests.connectors.fake_yandex import (
    CLIENT_ID,
    CLIENT_SECRET,
    OAUTH_SERVER,
    FakeYandex,
    sample_yandex,
)
from tests.fake_connector import public_resolver

KEY = Fernet.generate_key().decode()


@pytest.fixture
def server() -> FakeYandex:
    return sample_yandex()


def make_service(session: AsyncSession, http: OutboundClient) -> ConnectorService:
    settings = ConnectorSettings(
        secrets_keys=KEY,
        yandex_oauth_server=OAUTH_SERVER,
        bitrix24_oauth_server=BITRIX_OAUTH_SERVER,
    )  # type: ignore[arg-type]
    return ConnectorService(
        ConnectorRepository(session),
        GrantRepository(session),
        SyncRunRepository(session),
        ConnectorSyncJobRepository(session),
        MaterialRepository(session),
        AuditRepository(session),
        SecretBox([KEY]),
        default_registry(settings),
        settings,
        session,
        http,
        resolver=public_resolver,
    )


async def make_connector(session: AsyncSession) -> Connector:
    connector = Connector(
        kind="yandex360",
        name="Яндекс 360",
        mode=ConnectorMode.PER_USER.value,
        modules=["disk"],
        config={"client_id": CLIENT_ID},
        credentials=SecretBox([KEY]).encrypt({"client_secret": CLIENT_SECRET}),
        credentials_set_at=datetime.now(UTC),
    )
    session.add(connector)
    await session.commit()
    return connector


async def grant_device_token(
    session: AsyncSession,
    server: FakeYandex,
    connector: Connector,
    user: User,
    *,
    status: GrantStatus = GrantStatus.ACTIVE,
) -> str:
    """Грант с токеном, выданным для устройства (его можно отозвать)."""
    token = f"y0-device-{user.id}"
    server.access_tokens[token] = str(user.id)
    server.device_tokens.add(token)
    session.add(
        ConnectorUserGrant(
            connector_id=connector.id,
            user_id=user.id,
            credentials=SecretBox([KEY]).encrypt(
                {"access_token": token, "refresh_token": "r", "expires_at": "0"}
            ),
            status=status.value,
        )
    )
    await session.commit()
    return token


def revoked(server: FakeYandex) -> list[dict[str, str]]:
    return [form for method, form in server.calls if method == "oauth/revoke_token"]


async def grants_of(session: AsyncSession, connector_id: Any) -> list[Any]:
    return list(
        await session.scalars(
            select(ConnectorUserGrant.id).where(
                ConnectorUserGrant.connector_id == connector_id
            )
        )
    )


async def test_deleting_a_connector_revokes_every_employee_token(
    session: AsyncSession,
    tenant_ctx: Tenant,
    admin: User,
    employee: User,
    server: FakeYandex,
) -> None:
    with tenant_scope(tenant_ctx.id):
        connector = await make_connector(session)
        connector_id = connector.id
        first = await grant_device_token(session, server, connector, employee)
        # Грант, отвергнутый источником, — токен у провайдера может ещё жить.
        second = await grant_device_token(
            session, server, connector, admin, status=GrantStatus.EXPIRED
        )
        service = make_service(session, server.client())

        await service.delete(admin, connector_id)

        assert await session.get(Connector, connector_id) is None
    forms = revoked(server)
    assert sorted(form["access_token"] for form in forms) == sorted([first, second])
    assert all(form["client_secret"] == CLIENT_SECRET for form in forms)
    assert first not in server.access_tokens
    assert second not in server.access_tokens


@pytest.mark.parametrize(
    "failure",
    [
        (500, {"message": "internal"}),
        (400, {"error": "invalid_client", "error_description": "secret"}),
    ],
)
async def test_failed_revocation_does_not_block_deletion(
    session: AsyncSession,
    tenant_ctx: Tenant,
    admin: User,
    employee: User,
    server: FakeYandex,
    failure: tuple[int, dict[str, Any]],
) -> None:
    server.revoke_error = failure
    with tenant_scope(tenant_ctx.id):
        connector = await make_connector(session)
        connector_id = connector.id
        token = await grant_device_token(session, server, connector, employee)
        service = make_service(session, server.client())

        with capture_logs() as logs:
            await service.delete(admin, connector_id)

        assert await session.get(Connector, connector_id) is None
        assert await grants_of(session, connector_id) == []
    failed = [e for e in logs if e["event"] == "connector_token_revoke_failed"]
    assert len(failed) == 1
    assert failed[0]["error"] == "AdapterError"
    assert failed[0]["code"].startswith(("oauth_http_500", "oauth_revoke_invalid"))
    text = repr(logs)
    assert token not in text
    assert CLIENT_SECRET not in text
    assert "internal" not in text
    assert "secret" not in text.replace("client_secret", "")


async def test_network_error_does_not_block_deletion(
    session: AsyncSession,
    tenant_ctx: Tenant,
    admin: User,
    employee: User,
    server: FakeYandex,
) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused to y0-device", request=request)

    http = OutboundClient(
        httpx.AsyncClient(transport=httpx.MockTransport(broken)),
        resolver=public_resolver,
    )
    with tenant_scope(tenant_ctx.id):
        connector = await make_connector(session)
        connector_id = connector.id
        await grant_device_token(session, server, connector, employee)

        with capture_logs() as logs:
            await make_service(session, http).delete(admin, connector_id)

        assert await session.get(Connector, connector_id) is None
    failed = [e for e in logs if e["event"] == "connector_token_revoke_failed"]
    assert [(e["error"], e["code"]) for e in failed] == [
        ("AdapterError", "oauth_network_error")
    ]
    assert "y0-device" not in repr(logs)


async def test_slow_provider_does_not_hold_deletion(
    session: AsyncSession,
    tenant_ctx: Tenant,
    admin: User,
    employee: User,
    server: FakeYandex,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(connector_service, "REVOKE_TIMEOUT", 0.2)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={"status": "ok"})

    http = OutboundClient(
        httpx.AsyncClient(transport=httpx.MockTransport(slow)),
        resolver=public_resolver,
    )
    with tenant_scope(tenant_ctx.id):
        connector = await make_connector(session)
        connector_id = connector.id
        await grant_device_token(session, server, connector, employee)

        with capture_logs() as logs:
            async with asyncio.timeout(3):
                await make_service(session, http).delete(admin, connector_id)

        assert await session.get(Connector, connector_id) is None
    failed = [e for e in logs if e["event"] == "connector_token_revoke_failed"]
    assert [e["error"] for e in failed] == ["TimeoutError"]


async def test_token_without_device_is_logged_as_not_revocable(
    session: AsyncSession,
    tenant_ctx: Tenant,
    admin: User,
    employee: User,
    server: FakeYandex,
) -> None:
    with tenant_scope(tenant_ctx.id):
        connector = await make_connector(session)
        connector_id = connector.id
        token = await grant_device_token(session, server, connector, employee)
        server.device_tokens.discard(token)

        with capture_logs() as logs:
            await make_service(session, server.client()).delete(admin, connector_id)

        assert await session.get(Connector, connector_id) is None
    events = [e["event"] for e in logs]
    assert "connector_token_not_revocable" in events
    assert "connector_token_revoke_failed" not in events


async def test_employee_disconnecting_revokes_only_their_token(
    session: AsyncSession,
    tenant_ctx: Tenant,
    admin: User,
    employee: User,
    server: FakeYandex,
) -> None:
    with tenant_scope(tenant_ctx.id):
        connector = await make_connector(session)
        connector_id = connector.id
        mine = await grant_device_token(session, server, connector, employee)
        theirs = await grant_device_token(session, server, connector, admin)
        service = make_service(session, server.client())

        await service.revoke_my_credentials(employee, connector_id)

        assert len(await grants_of(session, connector_id)) == 1
    assert [form["access_token"] for form in revoked(server)] == [mine]
    assert theirs in server.access_tokens


async def test_failed_revocation_does_not_block_disconnecting(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    server: FakeYandex,
) -> None:
    server.revoke_error = (503, {"message": "maintenance"})
    with tenant_scope(tenant_ctx.id):
        connector = await make_connector(session)
        connector_id = connector.id
        await grant_device_token(session, server, connector, employee)

        with capture_logs() as logs:
            await make_service(session, server.client()).revoke_my_credentials(
                employee, connector_id
            )

        assert await grants_of(session, connector_id) == []
    failed = [e for e in logs if e["event"] == "connector_token_revoke_failed"]
    assert [e["code"] for e in failed] == ["oauth_http_503"]


async def test_bitrix24_has_nothing_to_revoke(
    session: AsyncSession,
    tenant_ctx: Tenant,
    admin: User,
    employee: User,
) -> None:
    portal = sample_portal()
    with tenant_scope(tenant_ctx.id):
        connector = Connector(
            kind="bitrix24",
            name="Портал",
            mode=ConnectorMode.PER_USER.value,
            modules=["disk"],
            config={"portal": portal.portal, "client_id": BITRIX_CLIENT_ID},
            credentials=SecretBox([KEY]).encrypt(
                {"client_secret": BITRIX_CLIENT_SECRET}
            ),
        )
        session.add(connector)
        await session.commit()
        connector_id = connector.id
        session.add(
            ConnectorUserGrant(
                connector_id=connector_id,
                user_id=employee.id,
                credentials=SecretBox([KEY]).encrypt(
                    {"access_token": "a", "refresh_token": "r", "expires_at": "0"}
                ),
            )
        )
        await session.commit()

        with capture_logs() as logs:
            await make_service(session, portal.client()).delete(admin, connector_id)

        assert await session.get(Connector, connector_id) is None
    assert portal.calls == []
    assert "connector_token_revoke_failed" not in [e["event"] for e in logs]
