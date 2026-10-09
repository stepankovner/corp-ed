"""HTTP-клиент WebDAV поверх OutboundClient: PROPFIND и GET.

- PROPFIND только с Depth: 0 или 1 (RFC 4918 §9.1): Depth: infinity
  серверы обычно запрещают, а на большом дереве это гигабайтный ответ.
  Тело ответа — не больше MAX_LISTING_BYTES; больше — response_too_large;
- редиректы не следуются: PROPFIND, ушедший на другой адрес, — признак
  неверного адреса в настройках (redirected), а GET файла уходит только
  на тот же хост (OutboundClient.download(same_host=True));
- 401 → один повтор после продления токена (OAuth Nextcloud), иначе
  auth_failed; 403 → forbidden; 404 → not_found; 405 → not_webdav (по
  адресу нет WebDAV); 429/503 → ожидание по Retry-After (не дольше
  MAX_RETRY_AFTER), до трёх попыток, затем rate_limited с повтором
  задачи позже. Лимитов частоты, как у SaaS API, у Nextcloud и NAS нет,
  но защита от перебора паролей отвечает 429 (Nextcloud bruteforce
  protection) — поэтому ждём, а не падаем сразу.

Тела ответов и заголовки с учётными данными не попадают ни в журнал, ни
в коды ошибок.
"""

import asyncio
import base64
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import urlsplit

import httpx

from corp_ed.connectors.base import AdapterAuthError, AdapterError
from corp_ed.connectors.common import Recorder
from corp_ed.connectors.webdav.multistatus import (
    PROPFIND_BODY,
    DavEntry,
    parse_multistatus,
)
from corp_ed.core.outbound import (
    OutboundClient,
    OutboundTooLargeError,
    OutboundURLError,
)

USER_AGENT = "corp-ed-connector/1.0"
REQUEST_TIMEOUT = 60.0
DOWNLOAD_TIMEOUT = 120.0
RATE_LIMIT_BACKOFF = (1.0, 2.0, 4.0)
MAX_RETRY_AFTER = 30.0
MAX_LISTING_BYTES = 16 * 1024 * 1024
"""Ответ PROPFIND на одну папку: ~0,5 КБ на элемент — около 30 тысяч
файлов в одной папке. Больше — ошибка, а не память воркера."""

Sleep = Callable[[float], Awaitable[None]]
Depth = Literal["0", "1"]


class DavAuth(Protocol):
    """Как подписывать запросы: пароль приложения (Basic) или токен OAuth."""

    async def header(self) -> str:
        """Значение Authorization (OAuth продлевает токен заранее)."""
        ...

    async def renew(self) -> bool:
        """Сервер ответил 401: продлить и повторить (True) или сдаться."""
        ...


@dataclass(frozen=True)
class BasicAuth:
    login: str
    password: str

    async def header(self) -> str:
        raw = f"{self.login}:{self.password}".encode()
        return "Basic " + base64.b64encode(raw).decode("ascii")

    async def renew(self) -> bool:
        return False


class WebDavClient:
    def __init__(
        self,
        http: OutboundClient,
        *,
        auth: DavAuth,
        sleep: Sleep = asyncio.sleep,
        recorder: Recorder | None = None,
    ) -> None:
        self._http = http
        self._auth = auth
        self._sleep = sleep
        self._recorder = recorder

    @property
    def auth(self) -> DavAuth:
        return self._auth

    async def propfind(
        self, url: str, *, depth: Depth, body: bytes = PROPFIND_BODY
    ) -> list[DavEntry]:
        backoff = iter(RATE_LIMIT_BACKOFF)
        renewed = False
        while True:
            headers = {
                "Authorization": await self._auth.header(),
                "Depth": depth,
                "Content-Type": "application/xml; charset=utf-8",
                "Accept": "application/xml, text/xml",
                "User-Agent": USER_AGENT,
            }
            try:
                response = await self._http.request(
                    "PROPFIND",
                    url,
                    headers=headers,
                    content=body,
                    timeout=REQUEST_TIMEOUT,
                    allow_redirects=False,
                    max_bytes=MAX_LISTING_BYTES,
                )
            except OutboundTooLargeError as exc:
                raise AdapterError("response_too_large") from exc
            except httpx.TimeoutException as exc:
                raise AdapterError("timeout", retryable=True) from exc
            except httpx.HTTPError as exc:
                raise AdapterError("network_error", retryable=True) from exc
            status = response.status_code
            if status == 401:
                if not renewed and await self._auth.renew():
                    renewed = True
                    continue
                raise AdapterAuthError("auth_failed")
            if status in (429, 503):
                delay = next(backoff, None)
                if delay is None:
                    raise AdapterError("rate_limited", retryable=True)
                await self._sleep(_retry_after(response, delay))
                continue
            if status in (200, 207):
                entries = parse_multistatus(response.content)
                if self._recorder is not None:
                    self._recorder(
                        "PROPFIND",
                        {"depth": depth, "path": urlsplit(url).path},
                        [_summary(entry) for entry in entries],
                    )
                return entries
            raise _status_error(status)

    async def download(self, url: str, *, max_bytes: int) -> bytes:
        backoff = iter(RATE_LIMIT_BACKOFF)
        renewed = False
        while True:
            headers = {
                "Authorization": await self._auth.header(),
                "Accept": "*/*",
                "User-Agent": USER_AGENT,
            }
            try:
                downloaded = await self._http.download(
                    url,
                    max_bytes=max_bytes,
                    headers=headers,
                    timeout=DOWNLOAD_TIMEOUT,
                    same_host=True,
                )
            except OutboundTooLargeError as exc:
                raise AdapterError("document_too_large") from exc
            except OutboundURLError as exc:
                if exc.code == "redirect_foreign":
                    raise AdapterError("download_redirect_foreign") from exc
                raise
            except httpx.TimeoutException as exc:
                raise AdapterError("timeout", retryable=True) from exc
            except httpx.HTTPError as exc:
                raise AdapterError("network_error", retryable=True) from exc
            status = downloaded.status_code
            if status == 401:
                if not renewed and await self._auth.renew():
                    renewed = True
                    continue
                raise AdapterAuthError("auth_failed")
            if status in (429, 503):
                delay = next(backoff, None)
                if delay is None:
                    raise AdapterError("rate_limited", retryable=True)
                await self._sleep(_retry_after_headers(downloaded.headers, delay))
                continue
            if status == 200:
                content_type = downloaded.headers.get("content-type", "").lower()
                if content_type.startswith("text/html"):
                    # Страница входа вместо файла: HTML среди читаемых
                    # форматов нет, настоящий документ так не придёт.
                    raise AdapterError("download_failed")
                return downloaded.content
            raise _status_error(status)


def _status_error(status: int) -> AdapterError:
    if 300 <= status < 400:
        return AdapterError("redirected")
    if status == 403:
        return AdapterError("forbidden")
    if status == 404:
        return AdapterError("not_found")
    if status in (405, 501):
        return AdapterError("not_webdav")
    return AdapterError(f"http_{status}", retryable=status >= 500)


def _retry_after(response: httpx.Response, default: float) -> float:
    return _retry_after_headers(response.headers, default)


def _retry_after_headers(headers: Mapping[str, str], default: float) -> float:
    value = headers.get("retry-after", "")
    if value.isdigit():
        return min(float(value), MAX_RETRY_AFTER)
    return default


def _summary(entry: DavEntry) -> dict[str, object]:
    return {
        "href": entry.href,
        "collection": entry.is_collection,
        "etag": entry.etag,
        "modified": entry.modified_raw,
        "size": entry.size,
        "fileid": entry.file_id,
    }
