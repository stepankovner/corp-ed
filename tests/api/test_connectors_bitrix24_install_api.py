"""Путь установки локального приложения Битрикс24 (/connectors/bitrix24/install).

Формы запросов — по документации Битрикс24 (apidocs.bitrix24.ru,
local-integrations/serverside-local-app-with-no-ui.html и
settings/app-installation/local-apps/installation-callback.html): при
установке портал шлёт POST со своего сервера — событие ONAPPINSTALL с
auth[access_token], auth[refresh_token], auth[member_id]… (приложение без
интерфейса) либо AUTH_ID, REFRESH_ID, member_id, APPLICATION_TOKEN
(приложение с интерфейсом). Это токены администратора портала: ответ 200,
ничего не пишем и не логируем."""

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from corp_ed.api.v1.rate_limits import CONNECTOR_BITRIX24_INSTALL_PER_IP
from corp_ed.domain.models import (
    AuditEvent,
    Connector,
    ConnectorSyncJob,
    ConnectorUserGrant,
)
from corp_ed.main import app

INSTALL_URL = "/api/v1/connectors/bitrix24/install"
SECRET_MARKERS = ("adm-access-7f3a", "adm-refresh-91c2", "adm-app-token-55d0")

ONAPPINSTALL_FORM = {
    "event": "ONAPPINSTALL",
    "auth[access_token]": SECRET_MARKERS[0],
    "auth[refresh_token]": SECRET_MARKERS[1],
    "auth[application_token]": SECRET_MARKERS[2],
    "auth[domain]": "portal.bitrix24.ru",
    "auth[member_id]": "member-1",
    "auth[scope]": "disk,landing",
    "auth[client_endpoint]": "https://portal.bitrix24.ru/rest/",
    "auth[server_endpoint]": "https://oauth.bitrix.info/rest/",
    "auth[expires_in]": "3600",
    "auth[status]": "L",
}
UI_FORM = {
    "AUTH_ID": SECRET_MARKERS[0],
    "REFRESH_ID": SECRET_MARKERS[1],
    "APPLICATION_TOKEN": SECRET_MARKERS[2],
    "APPLICATION_SCOPE": "disk,landing",
    "member_id": "member-1",
    "AUTH_EXPIRES": "3600",
    "status": "L",
    "PLACEMENT": "DEFAULT",
}


async def _row_counts(session: AsyncSession) -> list[int]:
    counts = []
    for model in (Connector, ConnectorUserGrant, ConnectorSyncJob, AuditEvent):
        result = await session.execute(select(func.count()).select_from(model))
        counts.append(result.scalar_one())
    return counts


@pytest.mark.parametrize("form", [ONAPPINSTALL_FORM, UI_FORM], ids=["event", "ui"])
async def test_install_post_answers_200_and_stores_nothing(
    api: httpx.AsyncClient, session: AsyncSession, form: dict[str, str]
) -> None:
    before = await _row_counts(session)
    with capture_logs() as logs:
        response = await api.post(
            f"{INSTALL_URL}?DOMAIN=portal.bitrix24.ru&APP_SID=abc", data=form
        )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    assert "приложение установлено" in response.text
    assert await _row_counts(session) == before
    dumped = repr(logs) + response.text + repr(response.headers)
    for marker in SECRET_MARKERS:
        assert marker not in dumped


async def test_install_get_answers_200(api: httpx.AsyncClient) -> None:
    response = await api.get(INSTALL_URL)
    assert response.status_code == 200
    assert "kronto" in response.text


async def test_install_post_with_json_and_empty_body(api: httpx.AsyncClient) -> None:
    for kwargs in ({}, {"json": {"auth": {"access_token": SECRET_MARKERS[0]}}}):
        response = await api.post(INSTALL_URL, **kwargs)
        assert response.status_code == 200
        assert SECRET_MARKERS[0] not in response.text


async def test_install_page_keeps_strict_security_headers(
    api: httpx.AsyncClient,
) -> None:
    # Запрос идёт с сервера портала, в iframe страница не нужна: общая
    # политика сайта остаётся как есть, без своего исключения.
    response = await api.post(INSTALL_URL, data=ONAPPINSTALL_FORM)
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"


def test_install_is_not_in_openapi_schema() -> None:
    assert not any("bitrix24/install" in path for path in app.openapi()["paths"])


async def test_install_is_rate_limited_by_ip(api: httpx.AsyncClient) -> None:
    limit = CONNECTOR_BITRIX24_INSTALL_PER_IP.limit
    statuses = [
        (await api.post(INSTALL_URL, data=UI_FORM)).status_code
        for _ in range(limit + 1)
    ]
    assert statuses[:limit] == [200] * limit
    assert statuses[limit] == 429
