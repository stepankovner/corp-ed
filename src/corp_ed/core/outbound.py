"""Исходящие запросы к системам клиентов: защита от SSRF.

Адрес портала Битрикс24 или Confluence задаёт администратор компании
— это URL от пользователя, и воркер пошёл бы по нему изнутри нашей
сети: к метаданным облака (169.254.169.254), к Redis и Postgres, к
соседним сервисам. Правила (OWASP SSRF Prevention):

- только https и без учётных данных в URL;
- имя резолвится ЗДЕСЬ, и каждый адрес проверяется: публичный, не
  loopback, не частный (RFC 1918, ULA), не link-local, не multicast,
  не служебный; IPv6 с вложенным IPv4 (mapped, 6to4, Teredo)
  проверяется по вложенному адресу;
- запрос уходит на ПРОВЕРЕННЫЙ адрес (IP в URL, имя — в Host и SNI),
  а не на имя, которое DNS мог подменить между проверкой и подключением
  (DNS rebinding);
- редиректы не следуются автоматически: каждый новый адрес проходит ту
  же проверку, не больше MAX_REDIRECTS.

Одна функция для всех адаптеров, с тестами на каждый класс адресов
(tests/security/test_outbound.py). Вторая линия — egress-политика
воркера в проде (DEPLOY.md).
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str], Awaitable[list[str]]]

MAX_REDIRECTS = 3
MAX_URL_LENGTH = 2048
# Заголовки с учётными данными не уходят на другой хост при редиректе:
# токен служебной учётки Confluence не должен уехать на CDN или чужой
# сервер, куда система клиента вдруг перенаправила скачивание.
_CREDENTIAL_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})


class OutboundURLError(ValueError):
    """Адрес не прошёл проверку. code — для клиента и журнала."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class OutboundTooLargeError(Exception):
    """Тело ответа больше лимита: чтение оборвано, остаток не скачан."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"response body exceeds {limit} bytes")
        self.limit = limit


@dataclass(frozen=True)
class Downloaded:
    status_code: int
    headers: httpx.Headers
    content: bytes


@dataclass(frozen=True)
class OutboundTarget:
    url: str
    """Нормализованный URL с именем хоста — для журнала и повторов."""
    host: str
    port: int
    address: str
    """Проверенный IP, к которому идёт подключение."""

    @property
    def pinned_url(self) -> str:
        parts = urlsplit(self.url)
        host = f"[{self.address}]" if ":" in self.address else self.address
        netloc = host if self.port == 443 else f"{host}:{self.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))

    @property
    def host_header(self) -> str:
        return self.host if self.port == 443 else f"{self.host}:{self.port}"


async def system_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    seen: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in seen:
            seen.append(address)
    return seen


def is_public_address(address: IPAddress) -> bool:
    """Публичный адрес, к которому воркеру можно подключаться."""
    if isinstance(address, ipaddress.IPv6Address):
        embedded = address.ipv4_mapped or address.sixtofour or address.teredo
        if isinstance(embedded, tuple):
            # Teredo: (сервер, клиент) — оба должны быть публичными.
            return all(is_public_address(part) for part in embedded)
        if embedded is not None:
            return is_public_address(embedded)
    return (
        address.is_global
        and not address.is_multicast
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_reserved
        and not address.is_unspecified
    )


async def validate_outbound_url(
    url: str, *, resolver: Resolver | None = None
) -> OutboundTarget:
    """Проверить URL и вернуть цель с закреплённым адресом.

    Выбрасывает OutboundURLError с кодом: invalid_url, scheme_not_https,
    credentials_in_url, host_not_resolved, address_not_public.
    """
    if not isinstance(url, str) or len(url) > MAX_URL_LENGTH:
        raise OutboundURLError("invalid_url")
    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        raise OutboundURLError("invalid_url") from exc
    if parts.scheme.lower() != "https":
        raise OutboundURLError("scheme_not_https")
    if parts.username is not None or parts.password is not None:
        raise OutboundURLError("credentials_in_url")
    host = (parts.hostname or "").strip(".").lower()
    if not host or len(host) > 253:
        raise OutboundURLError("invalid_url")
    try:
        port = parts.port or 443
    except ValueError as exc:
        raise OutboundURLError("invalid_url") from exc
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise OutboundURLError("address_not_public")

    try:
        literal: IPAddress | None = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [str(literal)]
    else:
        addresses = await (resolver or system_resolver)(host)
    if not addresses:
        raise OutboundURLError("host_not_resolved")
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw.split("%")[0])
        except ValueError as exc:
            raise OutboundURLError("address_not_public") from exc
        if not is_public_address(address):
            raise OutboundURLError("address_not_public")

    netloc = f"[{host}]" if ":" in host else host
    if port != 443:
        netloc = f"{netloc}:{port}"
    normalized = urlunsplit(("https", netloc, parts.path or "/", parts.query, ""))
    return OutboundTarget(url=normalized, host=host, port=port, address=addresses[0])


def _without_credentials(headers: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _CREDENTIAL_HEADERS}


class OutboundClient:
    """httpx-клиент, который ходит только по проверенным адресам.

    Адаптеры получают его вместо голого httpx.AsyncClient и не могут
    случайно обойти проверку: другого способа сделать запрос у них нет.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        resolver: Resolver | None = None,
        max_redirects: int = MAX_REDIRECTS,
    ) -> None:
        self._client = client
        self._resolver = resolver
        self._max_redirects = max_redirects

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        allow_redirects: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        """allow_redirects=False — вернуть ответ-редирект как есть: REST
        Битрикс24 отвечает 301/302 при смене адреса портала, и повторять
        POST как GET (без тела) значит получить ошибку метода вместо
        понятного «портал переехал»."""
        current = url
        request_headers = dict(headers or {})
        origin: str | None = None
        for _ in range(self._max_redirects + 1):
            target = await validate_outbound_url(current, resolver=self._resolver)
            origin = origin or target.host
            if target.host != origin:
                request_headers = _without_credentials(request_headers)
            response = await self._client.request(
                method,
                target.pinned_url,
                headers={**request_headers, "Host": target.host_header},
                timeout=timeout,
                follow_redirects=False,
                extensions={"sni_hostname": target.host},
                **kwargs,
            )
            if not allow_redirects:
                return response
            if response.is_redirect and "location" in response.headers:
                await response.aclose()
                current = urljoin(target.url, response.headers["location"])
                # После редиректа тело и метод не повторяются: адаптеры
                # ходят GET/POST к API, где редиректы — признак смены
                # домена портала, а не часть протокола.
                if response.status_code in (301, 302, 303):
                    method = "GET"
                    kwargs.pop("content", None)
                    kwargs.pop("json", None)
                    kwargs.pop("data", None)
                continue
            return response
        raise OutboundURLError("too_many_redirects")

    async def download(
        self,
        url: str,
        *,
        max_bytes: int,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> Downloaded:
        """GET с потоковым чтением тела не больше max_bytes.

        Файл из системы клиента читается кусками и обрывается, как
        только превысил лимит: портал, который отдаёт гигабайт вместо
        документа, не займёт память воркера. Заголовок Content-Length
        проверяется до чтения — но ему нельзя верить, поэтому считаем и
        сами. Редиректы — как в request: каждый адрес проверяется.
        """
        current = url
        request_headers = dict(headers or {})
        origin: str | None = None
        for _ in range(self._max_redirects + 1):
            target = await validate_outbound_url(current, resolver=self._resolver)
            origin = origin or target.host
            if target.host != origin:
                request_headers = _without_credentials(request_headers)
            request = self._client.build_request(
                "GET",
                target.pinned_url,
                headers={**request_headers, "Host": target.host_header},
                timeout=timeout,
                extensions={"sni_hostname": target.host},
            )
            response = await self._client.send(request, stream=True)
            try:
                if response.is_redirect and "location" in response.headers:
                    current = urljoin(target.url, response.headers["location"])
                    continue
                length = response.headers.get("content-length")
                if length is not None and length.isdigit() and int(length) > max_bytes:
                    raise OutboundTooLargeError(max_bytes)
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise OutboundTooLargeError(max_bytes)
                    chunks.append(chunk)
                return Downloaded(
                    response.status_code, response.headers, b"".join(chunks)
                )
            finally:
                await response.aclose()
        raise OutboundURLError("too_many_redirects")

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)
