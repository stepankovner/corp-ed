"""SSRF: адреса систем клиентов проверяются до подключения и закрепляются.

Каждый класс адресов — отдельный случай: loopback, частные, link-local
(метаданные облака), CGNAT, multicast, IPv6 со вложенным IPv4. Плюс
DNS rebinding: запрос уходит на проверенный IP, а не на имя.
"""

import ipaddress
from collections.abc import AsyncIterator

import httpx
import pytest

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
    ],
)
def test_non_public_addresses_are_rejected(address: str) -> None:
    assert not is_public_address(ipaddress.ip_address(address))


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
