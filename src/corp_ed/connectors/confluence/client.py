"""HTTP-клиент REST Confluence Server / DC поверх OutboundClient.

Авторизация — персональный токен (Bearer, DC 7.9+) или логин и пароль
служебной учётной записи (Basic). Списки — start/limit, конец по
`size < limit` или отсутствию `_links.next`. 429 (лимит DC) — ожидание
по Retry-After, до трёх попыток. Скачивание вложений — только с хоста
Confluence, потоково с обрывом по лимиту.
"""

import asyncio
import base64
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from corp_ed.connectors.base import AdapterAuthError, AdapterError
from corp_ed.core.outbound import OutboundClient, OutboundTooLargeError

USER_AGENT = "corp-ed-connector/1.0"
PAGE_LIMIT = 50
GROUP_LIMIT = 200
REQUEST_TIMEOUT = 30.0
DOWNLOAD_TIMEOUT = 120.0
RATE_LIMIT_BACKOFF = (1.0, 2.0, 4.0)
MAX_RETRY_AFTER = 30.0

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class TokenAuth:
    token: str

    def header(self) -> str:
        return f"Bearer {self.token}"


@dataclass(frozen=True)
class BasicAuth:
    username: str
    password: str

    def header(self) -> str:
        raw = f"{self.username}:{self.password}".encode()
        return "Basic " + base64.b64encode(raw).decode("ascii")


class ConfluenceClient:
    def __init__(
        self,
        http: OutboundClient,
        *,
        base_url: str,
        auth: TokenAuth | BasicAuth,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._http = http
        self._base = base_url if base_url.endswith("/") else base_url + "/"
        self._host = (urlsplit(self._base).hostname or "").lower()
        self._auth = auth
        self._sleep = sleep

    @property
    def base_url(self) -> str:
        return self._base

    def absolute(self, link: str) -> str:
        """Ссылка из ответа (_links.webui, _links.download) → абсолютная
        на хосте Confluence. Чужой хост в ответе не принимается."""
        url = urljoin(self._base, link.lstrip("/") if link.startswith("/") else link)
        if (urlsplit(url).hostname or "").lower() != self._host:
            raise AdapterError("link_foreign")
        return url

    async def get(
        self, path: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """GET /rest/api/{path}. Ошибки — AdapterError с кодом."""
        url = f"{self._base}rest/api/{path.lstrip('/')}"
        backoff = iter(RATE_LIMIT_BACKOFF)
        while True:
            try:
                response = await self._http.get(
                    url,
                    params=dict(params or {}),
                    headers=self._headers(accept="application/json"),
                    timeout=REQUEST_TIMEOUT,
                )
            except httpx.TimeoutException as exc:
                raise AdapterError("timeout", retryable=True) from exc
            except httpx.HTTPError as exc:
                raise AdapterError("network_error", retryable=True) from exc
            if response.status_code == 429:
                delay = next(backoff, None)
                if delay is None:
                    raise AdapterError("rate_limited", retryable=True)
                await self._sleep(_retry_after(response, delay))
                continue
            return _parse(response)

    async def paginate(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        limit: int = PAGE_LIMIT,
    ) -> AsyncIterator[dict[str, Any]]:
        start = 0
        while True:
            page = await self.get(
                path, {**(params or {}), "start": start, "limit": limit}
            )
            results = page.get("results")
            items = (
                [r for r in results if isinstance(r, dict)]
                if (isinstance(results, list))
                else []
            )
            for item in items:
                yield item
            raw_links = page.get("_links")
            links: dict[str, Any] = raw_links if isinstance(raw_links, dict) else {}
            if not items or (len(items) < limit and not links.get("next")):
                return
            start += len(items)

    async def download(self, link: str, *, max_bytes: int) -> bytes:
        url = self.absolute(link)
        try:
            downloaded = await self._http.download(
                url,
                max_bytes=max_bytes,
                headers=self._headers(accept="*/*"),
                timeout=DOWNLOAD_TIMEOUT,
            )
        except OutboundTooLargeError as exc:
            raise AdapterError("document_too_large") from exc
        except httpx.TimeoutException as exc:
            raise AdapterError("timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise AdapterError("network_error", retryable=True) from exc
        if downloaded.status_code == 401:
            raise AdapterAuthError("unauthorized")
        if downloaded.status_code != 200:
            raise AdapterError(
                f"download_http_{downloaded.status_code}",
                retryable=downloaded.status_code >= 500,
            )
        if downloaded.headers.get("content-type", "").lower().startswith("text/html"):
            # Страница входа вместо файла: SSO или нет прав.
            raise AdapterError("download_failed")
        return downloaded.content

    def _headers(self, *, accept: str) -> dict[str, str]:
        return {
            "Authorization": self._auth.header(),
            "Accept": accept,
            "User-Agent": USER_AGENT,
            # Confluence отвечает 403 XSRF на запросы без этого заголовка
            # из «браузероподобных» клиентов; REST его ожидает.
            "X-Atlassian-Token": "no-check",
        }


def _parse(response: httpx.Response) -> dict[str, Any]:
    status = response.status_code
    if status == 401:
        raise AdapterAuthError("unauthorized")
    data: Any
    try:
        data = response.json()
    except ValueError:
        data = None
    if status == 200:
        if not isinstance(data, dict):
            # HTML вместо JSON — страница входа SSO или не тот адрес.
            raise AdapterError("not_json")
        return data
    if status == 403:
        raise AdapterError("forbidden")
    if status == 404:
        raise AdapterError("not_found")
    if isinstance(data, dict) and isinstance(data.get("message"), str):
        # Confluence кладёт причину в message; в код берём только статус.
        pass
    raise AdapterError(f"http_{status}", retryable=status >= 500)


def _retry_after(response: httpx.Response, default: float) -> float:
    value = response.headers.get("retry-after", "")
    if value.isdigit():
        return min(float(value), MAX_RETRY_AFTER)
    return default
