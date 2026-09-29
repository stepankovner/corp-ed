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

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import MaterialStatus, Tenant, User
from corp_ed.domain.types import (
    ConnectorStatus,
    MaterialVisibility,
    SyncRunStatus,
    SyncTrigger,
)
from tests.live.bitrix24_stand import HAVE_PORTAL, Bitrix24Stand
from tests.test_connector_sync import access_of, materials_of, reload

pytestmark = pytest.mark.skipif(
    not HAVE_PORTAL,
    reason="нужны BITRIX24_TEST_PORTAL и BITRIX24_TEST_WEBHOOK",
)


async def test_personal_disk_is_synced_from_the_real_portal(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    stand = Bitrix24Stand()
    connector = await stand.connect(session, employee)
    portal = stand.portal

    async with httpx.AsyncClient() as raw:
        service = stand.sync_service(raw, session_maker)
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
