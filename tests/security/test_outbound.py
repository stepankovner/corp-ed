"""SSRF: адреса систем клиентов проверяются до подключения и закрепляются.

Каждый класс адресов — отдельный случай: loopback, частные, link-local
(метаданные облака), CGNAT, multicast, IPv6 со вложенным IPv4. Плюс
DNS rebinding: запрос уходит на проверенный IP, а не на имя.
"""

import asyncio
import gzip
import ipaddress
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from corp_ed.api.v1.dependencies import get_outbound_client, get_outbound_http_client
from corp_ed.core import outbound
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.outbound import (
    OutboundClient,
    OutboundTooLargeError,
    OutboundURLError,
    is_public_address,
    validate_outbound_url,
)

PUBLIC = "93.184.216.34"


def resolver_for(*addresses: str):  # type: ignore[no-untyped-def]
    async def resolve(host: str) -> list[str]:
        return list(addresses)

    return resolve


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.10.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.1",
        "169.254.169.254",  # метаданные облака
        "100.64.0.1",  # CGNAT
        "0.0.0.0",  # noqa: S104 — проверяем, что отвергается
        "224.0.0.1",
        "255.255.255.255",
        "192.0.2.1",  # TEST-NET
        "::1",
        "::",
        "fe80::1",
        "fc00::1",
        "fd12::1",
        "ff02::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "2002:7f00:1::",  # 6to4 → 127.0.0.1
        "2002:a00:1::",  # 6to4 → 10.0.0.1
        "2001:0:0:0:0:0:7f00:1",  # Teredo с частным адресом
        "64:ff9b::7f00:1",  # NAT64 → 127.0.0.1
        "64:ff9b::a00:1",  # NAT64 → 10.0.0.1
        "64:ff9b::a9fe:a9fe",  # NAT64 → 169.254.169.254
        "64:ff9b:1::a00:1",  # NAT64 local-use
        "::7f00:1",  # IPv4-compatible → 127.0.0.1
        "::a9fe:a9fe",  # IPv4-compatible → 169.254.169.254
        "::ffff:0:a00:1",  # IPv4-translated → 10.0.0.1
    ],
)
def test_non_public_addresses_are_rejected(address: str) -> None:
    assert not is_public_address(ipaddress.ip_address(address))


@pytest.mark.parametrize(
    "address",
    [
        "64:ff9b::808:808",
        "64:ff9b:1::808:808",
        "::808:808",
        "::ffff:0:808:808",
    ],
)
def test_ipv4_translation_prefixes_are_rejected_whatever_they_embed(
    address: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NAT64 и IPv4-compatible: настоящий адрес назначения выбирает
    транслятор, а не мы. Такие адреса отвергаются сами по себе, а не
    потому, что таблица ipaddress сегодня считает ::/8 зарезервированным."""
    monkeypatch.setattr(ipaddress.IPv6Address, "is_reserved", property(lambda _: False))
    monkeypatch.setattr(ipaddress.IPv6Address, "is_private", property(lambda _: False))
    monkeypatch.setattr(ipaddress.IPv6Address, "is_global", property(lambda _: True))
    assert not is_public_address(ipaddress.ip_address(address))
    assert is_public_address(ipaddress.ip_address("2001:4860:4860::8888"))


@pytest.mark.parametrize("address", [PUBLIC, "8.8.8.8", "2001:4860:4860::8888"])
def test_public_addresses_are_accepted(address: str) -> None:
    assert is_public_address(ipaddress.ip_address(address))


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://portal.example.com/rest", "scheme_not_https"),
        ("ftp://portal.example.com", "scheme_not_https"),
        ("portal.example.com", "scheme_not_https"),
        ("https://user:pass@portal.example.com", "credentials_in_url"),
        ("https://user@portal.example.com", "credentials_in_url"),
        ("https://localhost/", "address_not_public"),
        ("https://api.localhost/", "address_not_public"),
        ("https://db.internal/", "address_not_public"),
        ("https://printer.local/", "address_not_public"),
        ("https://127.0.0.1/", "address_not_public"),
        ("https://[::1]/", "address_not_public"),
        ("https://169.254.169.254/latest/meta-data/", "address_not_public"),
        ("https://[::ffff:169.254.169.254]/", "address_not_public"),
        ("https:///path", "invalid_url"),
        ("https://", "invalid_url"),
        ("https://portal.example.com:99999/", "invalid_url"),
        ("https://" + "a" * 3000 + ".com/", "invalid_url"),
    ],
)
async def test_bad_urls_are_rejected(url: str, code: str) -> None:
    with pytest.raises(OutboundURLError) as exc:
        await validate_outbound_url(url, resolver=resolver_for(PUBLIC))
    assert exc.value.code == code


async def test_hostname_resolving_to_private_address_is_rejected() -> None:
    with pytest.raises(OutboundURLError) as exc:
        await validate_outbound_url(
            "https://portal.example.com/", resolver=resolver_for("10.1.2.3")
        )
    assert exc.value.code == "address_not_public"


async def test_any_private_address_among_results_rejects_the_host() -> None:
    """Round-robin с одним частным адресом — атакующий дождётся его."""
    with pytest.raises(OutboundURLError):
        await validate_outbound_url(
            "https://portal.example.com/", resolver=resolver_for(PUBLIC, "127.0.0.1")
        )


async def test_unresolvable_host() -> None:
    with pytest.raises(OutboundURLError) as exc:
        await validate_outbound_url("https://nope.example/", resolver=resolver_for())
    assert exc.value.code == "host_not_resolved"


async def test_valid_url_is_normalized_and_pinned() -> None:
    target = await validate_outbound_url(
        "HTTPS://Portal.Example.com:443/rest/?a=1#frag", resolver=resolver_for(PUBLIC)
    )
    assert target.url == "https://portal.example.com/rest/?a=1"
    assert target.host == "portal.example.com"
    assert target.address == PUBLIC
    assert target.pinned_url == f"https://{PUBLIC}/rest/?a=1"
    assert target.host_header == "portal.example.com"


async def test_custom_port_and_ipv6_pinning() -> None:
    target = await validate_outbound_url(
        "https://portal.example.com:8443/x", resolver=resolver_for("2001:4860::1")
    )
    assert target.pinned_url == "https://[2001:4860::1]:8443/x"
    assert target.host_header == "portal.example.com:8443"


# --- OutboundClient -------------------------------------------------------------


def _client(handler):  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_request_goes_to_pinned_ip_with_host_and_sni() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    async with _client(handler) as raw:
        client = OutboundClient(raw, resolver=resolver_for(PUBLIC))
        response = await client.get("https://portal.example.com/rest/user.current")

    assert response.status_code == 200
    [request] = seen
    assert request.url.host == PUBLIC
    assert request.headers["host"] == "portal.example.com"
    assert request.extensions["sni_hostname"] == "portal.example.com"


async def test_via_proxy_sends_the_name_and_still_checks_the_address() -> None:
    """За egress-прокси запрос уходит по имени (прокси отвергает CONNECT
    к IP), но имя всё равно резолвится и проверяется."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    async with _client(handler) as raw:
        client = OutboundClient(raw, resolver=resolver_for(PUBLIC), via_proxy=True)
        response = await client.get("https://portal.example.com/rest/user.current")
        with pytest.raises(OutboundURLError) as exc:
            await OutboundClient(
                raw, resolver=resolver_for("10.0.0.5"), via_proxy=True
            ).get("https://intranet.example.com/")

    assert response.status_code == 200
    [request] = seen
    assert request.url.host == "portal.example.com"
    assert "sni_hostname" not in request.extensions
    assert exc.value.code == "address_not_public"


async def test_via_proxy_download_goes_by_name() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(200, content=b"data")

    client = OutboundClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        resolver=resolver_for(PUBLIC),
        via_proxy=True,
    )
    downloaded = await client.download("https://portal.example.com/f", max_bytes=100)
    assert downloaded.content == b"data"
    assert hosts == ["portal.example.com"]


async def test_private_url_never_reaches_the_transport() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    async with _client(handler) as raw:
        client = OutboundClient(raw, resolver=resolver_for("10.0.0.5"))
        with pytest.raises(OutboundURLError):
            await client.get("https://portal.example.com/")
    assert not called


async def test_redirect_to_private_address_is_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"location": "https://169.254.169.254/latest/meta-data/"}
        )

    async with _client(handler) as raw:
        client = OutboundClient(raw, resolver=resolver_for(PUBLIC))
        with pytest.raises(OutboundURLError) as exc:
            await client.get("https://portal.example.com/")
    assert exc.value.code == "address_not_public"


async def test_redirect_to_public_host_is_revalidated_and_followed() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.headers["host"])
        if request.headers["host"] == "old.example.com":
            return httpx.Response(
                301, headers={"location": "https://new.example.com/x"}
            )
        return httpx.Response(200, text="moved")

    async with _client(handler) as raw:
        client = OutboundClient(raw, resolver=resolver_for(PUBLIC))
        response = await client.post("https://old.example.com/", json={"a": 1})
    assert response.text == "moved"
    assert hosts == ["old.example.com", "new.example.com"]


async def test_dns_rebinding_between_redirects_is_caught() -> None:
    """Первый ответ резолвера публичный, второй — частный: второе
    подключение не состоится."""
    answers = iter([[PUBLIC], ["127.0.0.1"]])

    async def flipping(host: str) -> list[str]:
        return next(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://portal.example.com/2"})

    async with _client(handler) as raw:
        client = OutboundClient(raw, resolver=flipping)
        with pytest.raises(OutboundURLError):
            await client.get("https://portal.example.com/1")


async def test_too_many_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://portal.example.com/"})

    async with _client(handler) as raw:
        client = OutboundClient(raw, resolver=resolver_for(PUBLIC), max_redirects=2)
        with pytest.raises(OutboundURLError) as exc:
            await client.get("https://portal.example.com/")
    assert exc.value.code == "too_many_redirects"


async def test_redirect_can_be_returned_as_is() -> None:
    """Адаптер Битрикс24 не следует редиректу: 301 — портал переехал."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["host"])
        return httpx.Response(301, headers={"location": "https://new.example.com/x"})

    client = OutboundClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        resolver=resolver_for(PUBLIC),
    )
    response = await client.post(
        "https://portal.example.com/rest/m", json={"a": 1}, allow_redirects=False
    )
    assert response.status_code == 301
    assert response.headers["location"] == "https://new.example.com/x"
    assert seen == ["portal.example.com"]


async def test_download_stops_reading_past_the_limit() -> None:
    """Тело больше лимита обрывается по ходу чтения, а не после."""
    served: list[int] = []

    async def body() -> AsyncIterator[bytes]:
        for index in range(100):
            served.append(index)
            yield b"x" * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_Stream(body()))

    client = OutboundClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        resolver=resolver_for(PUBLIC),
    )
    with pytest.raises(OutboundTooLargeError):
        await client.download("https://portal.example.com/f", max_bytes=4096)
    assert len(served) < 100

    small = await client.download("https://portal.example.com/f", max_bytes=1 << 20)
    assert small.status_code == 200
    assert len(small.content) == 100 * 1024


async def test_download_has_a_deadline_for_the_whole_file() -> None:
    """Портал отдаёт файл по капле: каждый кусок укладывается в таймаут
    чтения, но весь файл — нет. Скачивание обрывается по общему сроку."""
    served: list[int] = []

    async def body() -> AsyncIterator[bytes]:
        for index in range(1000):
            served.append(index)
            await asyncio.sleep(0.01)
            yield b"x"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_Stream(body()))

    client = OutboundClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        resolver=resolver_for(PUBLIC),
        download_deadline=0.2,
    )
    # Адаптеры уже переводят httpx.TimeoutException в свой код «таймаут».
    with pytest.raises(httpx.TimeoutException):
        await client.download("https://portal.example.com/f", max_bytes=1 << 20)
    assert len(served) < 1000


async def test_download_deadline_is_a_setting() -> None:
    assert outbound.DOWNLOAD_DEADLINE == 300.0
    settings = ConnectorSettings(environment="development")  # type: ignore[call-arg]
    assert settings.download_timeout_seconds == 300
    with pytest.raises(ValidationError):
        ConnectorSettings(environment="development", download_timeout_seconds=0)  # type: ignore[call-arg]


@pytest.mark.parametrize("via_proxy", [False, True])
async def test_outbound_http_client_reads_proxy_from_environment_only_by_flag(
    via_proxy: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTPS_PROXY в окружении не подхватывается молча: только с
    CONNECTOR_OUTBOUND_VIA_PROXY=true, когда прокси и задуман."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    client = outbound.outbound_http_client(via_proxy=via_proxy)
    try:
        assert client.trust_env is via_proxy
        mounts = client._mounts  # прокси из окружения httpx кладёт сюда
        assert bool(mounts) is via_proxy
    finally:
        await client.aclose()


async def test_api_connectors_use_the_outbound_client_with_its_deadline() -> None:
    """Ручки коннекторов ходят наружу через свой клиент, а не через общий
    клиент моделей, и с общим сроком скачивания из настроек."""
    models = httpx.AsyncClient()
    outside = outbound.outbound_http_client(via_proxy=False)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(http_client=models, outbound_http_client=outside)
        )
    )
    try:
        client = get_outbound_http_client(request)  # type: ignore[arg-type]
        assert client is outside
        built = get_outbound_client(client)
        assert built._client is outside
        assert built._download_deadline == 300
    finally:
        await models.aclose()
        await outside.aclose()


async def test_download_trusts_content_length_only_to_refuse_early() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": "999999"}, content=b"ok")

    client = OutboundClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        resolver=resolver_for(PUBLIC),
    )
    with pytest.raises(OutboundTooLargeError):
        await client.download("https://portal.example.com/f", max_bytes=10)


async def test_download_follows_checked_redirects() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.headers["host"])
        if request.headers["host"] == "portal.example.com":
            return httpx.Response(
                302, headers={"location": "https://cdn.example.com/f"}
            )
        return httpx.Response(200, content=b"data")

    client = OutboundClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        resolver=resolver_for(PUBLIC),
    )
    downloaded = await client.download("https://portal.example.com/f", max_bytes=100)
    assert downloaded.content == b"data"
    assert hosts == ["portal.example.com", "cdn.example.com"]


async def test_download_can_stay_on_the_same_host() -> None:
    """same_host: файл с WebDAV-сервера не уходит по редиректу на другой
    хост вовсе — ни с учётными данными, ни без них."""
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.headers["host"])
        if request.url.path == "/old":
            return httpx.Response(302, headers={"location": "/new"})
        if request.url.path == "/new":
            return httpx.Response(200, content=b"data")
        return httpx.Response(302, headers={"location": "https://cdn.example.com/f"})

    client = OutboundClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        resolver=resolver_for(PUBLIC),
    )
    downloaded = await client.download(
        "https://dav.example.com/old", max_bytes=100, same_host=True
    )
    assert downloaded.content == b"data"
    with pytest.raises(OutboundURLError) as exc:
        await client.download(
            "https://dav.example.com/away", max_bytes=100, same_host=True
        )
    assert exc.value.code == "redirect_foreign"
    assert hosts == ["dav.example.com"] * 3


class _Stream(httpx.AsyncByteStream):
    def __init__(self, chunks: AsyncIterator[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._chunks:
            yield chunk


async def test_credentials_are_dropped_on_cross_host_redirect() -> None:
    """Токен служебной учётки не уезжает на другой хост вслед за редиректом."""
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.headers["host"], request.headers.get("authorization")))
        if request.headers["host"] == "wiki.example.com":
            return httpx.Response(
                302, headers={"location": "https://cdn.example.com/f"}
            )
        return httpx.Response(200, content=b"data")

    client = OutboundClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        resolver=resolver_for(PUBLIC),
    )
    headers = {"Authorization": "Bearer secret", "Accept": "*/*"}
    downloaded = await client.download(
        "https://wiki.example.com/f", max_bytes=100, headers=headers
    )
    assert downloaded.content == b"data"
    response = await client.get("https://wiki.example.com/f", headers=headers)
    assert response.status_code == 200
    assert seen == [
        ("wiki.example.com", "Bearer secret"),
        ("cdn.example.com", None),
        ("wiki.example.com", "Bearer secret"),
        ("cdn.example.com", None),
    ]


async def test_credentials_survive_same_host_redirect() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        if request.url.path == "/old":
            return httpx.Response(
                302, headers={"location": "https://wiki.example.com/new"}
            )
        return httpx.Response(200, content=b"ok")

    client = OutboundClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        resolver=resolver_for(PUBLIC),
    )
    await client.get(
        "https://wiki.example.com/old", headers={"Authorization": "Bearer s"}
    )
    assert seen == ["Bearer s", "Bearer s"]


async def test_query_and_body_are_dropped_on_cross_host_redirect() -> None:
    """Секрет в параметрах запроса (client_secret, code) не уезжает на
    другой хост вслед за редиректом — ни при 302, ни при 307 с телом."""
    seen: list[tuple[str, str, str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.headers["host"]
        seen.append((host, request.method, request.url.query.decode(), request.content))
        if host == "oauth.example.com":
            status = 307 if request.method == "POST" else 302
            return httpx.Response(
                status, headers={"location": "https://other.example.com/token"}
            )
        return httpx.Response(200, json={"ok": True})

    client = OutboundClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        resolver=resolver_for(PUBLIC),
    )
    params = {"client_secret": "s3", "code": "c0"}
    response = await client.get("https://oauth.example.com/token", params=params)
    assert response.status_code == 200
    response = await client.post(
        "https://oauth.example.com/token", params=params, data={"code": "c0"}
    )
    assert response.status_code == 200
    assert seen == [
        ("oauth.example.com", "GET", "client_secret=s3&code=c0", b""),
        ("other.example.com", "GET", "", b""),
        ("oauth.example.com", "POST", "client_secret=s3&code=c0", b"code=c0"),
        ("other.example.com", "POST", "", b""),
    ]


async def test_query_survives_same_host_redirect() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.query.decode())
        if request.url.path == "/old":
            return httpx.Response(
                302, headers={"location": "https://wiki.example.com/new"}
            )
        return httpx.Response(200, content=b"ok")

    client = OutboundClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        resolver=resolver_for(PUBLIC),
    )
    response = await client.get("https://wiki.example.com/old", params={"page": "2"})
    assert response.status_code == 200
    assert seen == ["page=2", "page=2"]


# --- потолок тела ответа API --------------------------------------------------


def _chunked_client(served: list[int], chunks: int = 100) -> OutboundClient:
    async def body() -> AsyncIterator[bytes]:
        for index in range(chunks):
            served.append(index)
            yield b"x" * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_Stream(body()))

    return OutboundClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        resolver=resolver_for(PUBLIC),
    )


async def test_api_response_stops_reading_past_the_limit() -> None:
    """Портал, который отвечает на вызов API гигабайтом, не займёт память
    воркера: чтение обрывается по ходу, как у download."""
    served: list[int] = []
    client = _chunked_client(served)
    with pytest.raises(OutboundTooLargeError):
        await client.get("https://portal.example.com/rest/x", max_bytes=4096)
    assert len(served) < 100
    with pytest.raises(OutboundTooLargeError):
        await client.post("https://portal.example.com/rest/x", max_bytes=4096)

    response = await client.get("https://portal.example.com/rest/x")
    assert response.status_code == 200
    assert len(response.content) == 100 * 1024
    assert response.text == "x" * 100 * 1024


async def test_api_response_has_a_default_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert outbound.MAX_RESPONSE_BYTES >= 16 * 1024 * 1024
    monkeypatch.setattr(outbound, "MAX_RESPONSE_BYTES", 4096)
    client = _chunked_client([])
    with pytest.raises(OutboundTooLargeError) as exc:
        await client.get("https://portal.example.com/rest/x")
    assert exc.value.limit == 4096


async def test_api_response_content_length_refused_early() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": "999999"}, content=b"{}")

    client = OutboundClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        resolver=resolver_for(PUBLIC),
    )
    with pytest.raises(OutboundTooLargeError):
        await client.get("https://portal.example.com/rest/x", max_bytes=10)


async def test_api_response_under_the_ceiling_reads_as_usual() -> None:
    """Сжатый JSON (Accept-Encoding по умолчанию) читается как раньше:
    .json(), .text, заголовки и код ответа на месте."""
    payload = {"result": ["ё" * 10, {"next": 50}]}

    async def wire() -> AsyncIterator[bytes]:
        yield gzip.compress(json.dumps(payload).encode())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            headers={
                "content-encoding": "gzip",
                "content-type": "application/json; charset=utf-8",
                "retry-after": "3",
            },
            stream=_Stream(wire()),
        )

    client = OutboundClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        resolver=resolver_for(PUBLIC),
    )
    response = await client.get("https://portal.example.com/rest/x", max_bytes=4096)
    assert response.status_code == 201
    assert response.json() == payload
    assert json.loads(response.text) == payload
    assert response.headers["retry-after"] == "3"
    assert response.request.url.host == PUBLIC


async def test_redirect_returned_as_is_is_read_within_the_ceiling() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            301,
            headers={"location": "https://new.example.com/x"},
            content=b"m" * 5000,
        )

    client = OutboundClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        resolver=resolver_for(PUBLIC),
    )
    response = await client.post(
        "https://portal.example.com/rest/m", allow_redirects=False, max_bytes=8192
    )
    assert response.is_redirect
    assert response.text == "m" * 5000
    with pytest.raises(OutboundTooLargeError):
        await client.post(
            "https://portal.example.com/rest/m", allow_redirects=False, max_bytes=100
        )
