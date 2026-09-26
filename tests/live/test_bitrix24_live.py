"""Живая проверка Битрикс24: настоящий портал, настоящая песочница разбора.

Запускается только с переменными тестового портала (в CI их нет —
пропуск): BITRIX24_TEST_PORTAL, BITRIX24_TEST_WEBHOOK (вебхук с правами
disk и user); за egress-прокси (HTTPS_PROXY) запросы идут по имени, как с
CONNECTOR_OUTBOUND_VIA_PROXY=true в бою. Вебхук кладётся в грант
сотрудника напрямую — это обход OAuth только для стенда: адаптер
принимает вебхук наравне с токенами (build_client), а через API такой
грант не создать.

    BITRIX24_TEST_PORTAL=… BITRIX24_TEST_WEBHOOK=… uv run pytest tests/live -q
"""

import os
from datetime import UTC, datetime

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.registry import default_registry
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.outbound import OutboundClient, validate_outbound_url
from corp_ed.core.secrets import SecretBox
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import (
    Connector,
    ConnectorUserGrant,
    MaterialStatus,
    Tenant,
    User,
)
from corp_ed.domain.types import (
    ConnectorMode,
    ConnectorStatus,
    MaterialVisibility,
    SyncRunStatus,
    SyncTrigger,
)
from corp_ed.services.connector_sync_service import ConnectorSyncService
from tests.test_connector_sync import access_of, materials_of, reload

PORTAL = os.environ.get("BITRIX24_TEST_PORTAL", "")
WEBHOOK = os.environ.get("BITRIX24_TEST_WEBHOOK", "")
# За egress-прокси (HTTPS_PROXY) запросы идут по имени — как в бою с
# CONNECTOR_OUTBOUND_VIA_PROXY; явный флаг сильнее автоопределения.
_FLAG = os.environ.get("CONNECTOR_OUTBOUND_VIA_PROXY", "").lower()
VIA_PROXY = _FLAG in {"1", "true", "yes"} or (
    not _FLAG and bool(os.environ.get("HTTPS_PROXY"))
)

pytestmark = pytest.mark.skipif(
    not (PORTAL and WEBHOOK),
    reason="нужны BITRIX24_TEST_PORTAL и BITRIX24_TEST_WEBHOOK",
)


async def test_personal_disk_is_synced_from_the_real_portal(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    key = Fernet.generate_key().decode()
    secrets = SecretBox([key])
    settings = ConnectorSettings(secrets_keys=key, outbound_via_proxy=VIA_PROXY)  # type: ignore[arg-type]
    portal = (await validate_outbound_url(PORTAL)).url
    connector = Connector(
        kind="bitrix24",
        name="Битрикс24 (стенд)",
        mode=ConnectorMode.PER_USER.value,
        # Без модулей базы знаний: у вебхука стенда нет scope note, а
        # landing на пустом портале ничего не даёт.
        modules=["disk", "disk_personal"],
        config={"portal": portal, "client_id": "stand"},
        credentials=secrets.encrypt({"client_secret": "stand"}),
        credentials_set_at=datetime.now(UTC),
    )
    session.add(connector)
    await session.commit()
    session.add(
        ConnectorUserGrant(
            connector_id=connector.id,
            user_id=employee.id,
            credentials=secrets.encrypt({"webhook": WEBHOOK}),
            external_user_id="1",
        )
    )
    await session.commit()

    async with httpx.AsyncClient() as raw:
        service = ConnectorSyncService(
            session_maker,
            OutboundClient(raw, via_proxy=VIA_PROXY),
            default_registry(settings),
            secrets,
            settings,
        )
        outcome = await service.run(
            tenant_ctx.id, connector.id, trigger=SyncTrigger.MANUAL
        )
        assert outcome is not None
        assert outcome.status is SyncRunStatus.SUCCEEDED, outcome
        assert outcome.stats.grants == 1
        assert outcome.stats.added >= 1
        assert outcome.stats.failed == 0

        with tenant_scope(tenant_ctx.id):
            materials = await materials_of(session, connector)
            assert materials
            for external_id, material in materials.items():
                assert external_id.startswith("disk:")
                assert material.source_url is not None
                assert material.source_url.startswith(portal)
                assert material.status is MaterialStatus.PENDING
                # Текст извлечён настоящей песочницей из настоящего файла.
                assert material.content and len(material.content) > 100
                assert material.visibility == MaterialVisibility.RESTRICTED.value
                assert await access_of(session, material) == {employee.id}
            assert (await reload(session, connector)).status == (
                ConnectorStatus.ACTIVE.value
            )

        # Второй запуск: версии те же — ничего не скачивается заново.
        again = await service.run(
            tenant_ctx.id, connector.id, trigger=SyncTrigger.MANUAL
        )
        assert again is not None
        assert again.status is SyncRunStatus.SUCCEEDED
        assert again.stats.added == 0
        assert again.stats.updated == 0
