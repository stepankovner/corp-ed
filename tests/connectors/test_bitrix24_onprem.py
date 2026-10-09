"""Коробочный Битрикс24 тем же видом bitrix24: портал на домене компании.

Коробка работает с тем же REST и тем же сервером авторизации
oauth.bitrix24.tech, что облако (b24-rest-docs: settings/cloud-and-on-premise/
network-access, settings/oauth). Проверяем, что адрес вида
https://portal.company.ru/ (в том числе с портом) проходит форму, OAuth
уходит на сервер авторизации, а не на портал, и скачивание принимается
только с хоста самого портала.
"""

import pytest
from cryptography.fernet import Fernet

from corp_ed.connectors.base import AdapterError
from corp_ed.connectors.bitrix24 import SPEC
from corp_ed.connectors.bitrix24.adapter import Bitrix24Adapter, build_client
from corp_ed.connectors.registry import default_registry
from corp_ed.core.config import ConnectorSettings
from corp_ed.services.connector_service import (
    InvalidConnectorConfigError,
    _validate_config,
)
from tests.connectors.fake_portal import (
    ACCESS_TOKEN,
    AUTH_CODE,
    CLIENT_ID,
    CLIENT_SECRET,
    OAUTH_HOST,
    OAUTH_SERVER,
    REFRESH_TOKEN,
    FakePortal,
    sample_portal,
)
from tests.fake_connector import public_resolver

KEY = Fernet.generate_key().decode()
MAX_BYTES = 1024 * 1024
BOX_HOST = "portal.company.ru"


def settings() -> ConnectorSettings:
    return ConnectorSettings(
        secrets_keys=KEY,
        bitrix24_oauth_server=OAUTH_SERVER,
        max_document_bytes=MAX_BYTES,
    )  # type: ignore[arg-type]


def make_adapter(portal: FakePortal, **credentials: str) -> Bitrix24Adapter:
    client = build_client(
        {"portal": portal.portal, "client_id": CLIENT_ID},
        {
            "client_secret": CLIENT_SECRET,
            "access_token": ACCESS_TOKEN,
            "refresh_token": REFRESH_TOKEN,
            "expires_at": "0",
            **credentials,
        },
        portal.client(),
        settings(),
        min_interval=0.0,
    )
    return Bitrix24Adapter(client, max_bytes=MAX_BYTES)


async def private_resolver(host: str) -> list[str]:
    return ["10.0.0.5"]


def test_spec_names_cloud_and_box() -> None:
    assert SPEC.title == "Битрикс24 (облако и коробка)"
    portal_field = SPEC.config_fields[0]
    assert portal_field.name == "portal"
    assert "bitrix24.ru" in portal_field.title
    assert "коробка" in portal_field.title
    assert "из интернета" in portal_field.title


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://portal.company.ru/", "https://portal.company.ru/"),
        ("https://portal.company.ru", "https://portal.company.ru/"),
        ("https://portal.company.ru:8443/", "https://portal.company.ru:8443/"),
        ("https://b24-abc123.bitrix24.ru/", "https://b24-abc123.bitrix24.ru/"),
    ],
)
async def test_form_accepts_box_domain(value: str, expected: str) -> None:
    clean = await _validate_config(
        SPEC, {"portal": value, "client_id": CLIENT_ID}, public_resolver
    )
    assert clean == {"portal": expected, "client_id": CLIENT_ID}


@pytest.mark.parametrize(
    ("value", "resolver", "code"),
    [
        ("http://portal.company.ru/", public_resolver, "scheme_not_https"),
        ("https://portal.company.ru/", private_resolver, "address_not_public"),
        ("https://bitrix.local/", public_resolver, "address_not_public"),
        ("https://admin:pw@portal.company.ru/", public_resolver, "credentials_in_url"),
    ],
)
async def test_box_address_keeps_ssrf_checks(value: str, resolver, code: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(InvalidConnectorConfigError) as caught:
        await _validate_config(
            SPEC, {"portal": value, "client_id": CLIENT_ID}, resolver
        )
    assert caught.value.code == code


@pytest.mark.parametrize("host", [BOX_HOST, f"{BOX_HOST}:8443"])
async def test_box_portal_lists_and_downloads_from_its_own_host(host: str) -> None:
    portal = sample_portal(host)
    adapter = make_adapter(portal)
    await adapter.check()
    documents = {d.external_id: d async for d in adapter.list(["disk"])}
    assert documents["disk:102"].url.startswith(f"https://{host}/")

    fetched = await adapter.fetch(documents["disk:102"], max_bytes=MAX_BYTES)

    assert fetched.data == "Отпуск — 28 дней.".encode()  # type: ignore[union-attr]
    assert portal.downloads and portal.downloads[0].startswith("/rest/download.json")


async def test_box_rejects_download_link_to_another_host() -> None:
    """Ссылка из ответа коробки на облачный или любой другой хост — не
    скачивается: токен и воркер уходят только на сам портал."""
    portal = sample_portal(BOX_HOST)
    portal.download_host = "b24-abc123.bitrix24.ru"
    adapter = make_adapter(portal)
    documents = {d.external_id: d async for d in adapter.list(["disk"])}
    with pytest.raises(AdapterError, match="download_url_foreign"):
        await adapter.fetch(documents["disk:102"], max_bytes=MAX_BYTES)


async def test_box_oauth_goes_to_bitrix_auth_server_not_portal() -> None:
    portal = sample_portal(BOX_HOST)
    hosts: list[str] = []
    handle = portal.handle

    def spy(request):  # type: ignore[no-untyped-def]
        hosts.append(request.headers.get("host", ""))
        if request.headers.get("host") == BOX_HOST:
            # client_secret никогда не уходит на коробку клиента.
            assert CLIENT_SECRET not in str(request.url)
            assert CLIENT_SECRET.encode() not in request.content
        return handle(request)

    portal.handle = spy  # type: ignore[method-assign]
    registry = default_registry(settings())
    config = {"portal": portal.portal, "client_id": CLIENT_ID}
    flow = registry.build_oauth(
        "bitrix24", config, {"client_secret": CLIENT_SECRET}, portal.client()
    )

    assert flow.authorize_url("st").startswith(f"https://{BOX_HOST}/oauth/authorize/?")
    exchanged = await flow.exchange(AUTH_CODE)
    assert hosts == [OAUTH_HOST]

    # Истёкший токен продлевается тоже на сервере авторизации.
    portal.expired.add(exchanged.credentials["access_token"])
    adapter = make_adapter(portal, **exchanged.credentials)
    await adapter.check()
    assert hosts == [OAUTH_HOST, BOX_HOST, OAUTH_HOST, BOX_HOST]
    assert adapter.refreshed_credentials is not None
