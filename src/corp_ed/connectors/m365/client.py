"""HTTP-клиент Microsoft Graph v1.0 поверх OutboundClient.

Все запросы — `GET https://graph.microsoft.com/v1.0/…` с токеном
приложения (auth.py). Списки — `value` и абсолютная ссылка
`@odata.nextLink` на следующую страницу (learn.microsoft.com/graph/paging);
по ней клиент идёт, только если она ведёт на тот же адрес Graph: токен
не уходит на чужой хост, даже если ответ подменён. Ошибки —
`{error: {code, message, innerError}}` (learn.microsoft.com/graph/errors):
наружу идёт статус, code — только в журнал через safe_code, message
(там имена файлов и почты) — никуда.

Лимиты (learn.microsoft.com/graph/throttling и scan-guidance OneDrive):
429, 503 и 504 — подождать Retry-After (не больше MAX_RETRY_AFTER) или,
если его нет, экспоненциальную паузу; после RATE_LIMIT_BACKOFF попыток —
AdapterError('rate_limited', retryable): запуск повторится позже. 401 —
токен запрашивается заново один раз, второй 401 — AdapterAuthError.
410 — delta-ссылка устарела (resyncRequired): повтор запуска с начала.

Содержимое файла (`/content`) Graph отдаёт ответом 302 на
предварительно подписанный адрес `*.sharepoint.com` (driveitem-get-content:
«You don't need to include an Authorization header»): OutboundClient
проверяет новый адрес (https, публичный IP) и на другой хост не несёт
Authorization; параметр tempauth в самом адресе сохраняется.

В записи фикстур (`--record`) нет токенов: запрос токена не пишется, а
`@microsoft.graph.downloadUrl` (подписанная ссылка с tempauth)
вырезается из ответов целиком.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from typing import Any

import httpx
import structlog

from corp_ed.connectors.base import AdapterAuthError, AdapterError
from corp_ed.connectors.common import (
    Recorder,
    json_object,
    redact,
    retry_after,
    safe_code,
)
from corp_ed.connectors.m365.auth import ClientCredentials
from corp_ed.core.outbound import OutboundClient, OutboundTooLargeError

logger = structlog.get_logger()

GRAPH_ROOT = "https://graph.microsoft.com/v1.0/"
USER_AGENT = "corp-ed-connector/1.0"
REQUEST_TIMEOUT = 60.0
DOWNLOAD_TIMEOUT = 120.0
RATE_LIMIT_BACKOFF = (2.0, 4.0, 8.0, 16.0, 32.0)
MAX_RETRY_AFTER = 60.0
MAX_PAGES = 100_000
_RETRY_STATUSES = frozenset({429, 503, 504})
_DOWNLOAD_URL = "@microsoft.graph.downloadUrl"

Sleep = Callable[[float], Awaitable[None]]


class GraphClient:
    def __init__(
        self,
        http: OutboundClient,
        auth: ClientCredentials,
        *,
        root: str = GRAPH_ROOT,
        sleep: Sleep = asyncio.sleep,
        recorder: Recorder | None = None,
    ) -> None:
        self._http = http
        self._auth = auth
        self._root = root if root.endswith("/") else root + "/"
        self._sleep = sleep
        self._recorder = recorder

    @property
    def auth(self) -> ClientCredentials:
        return self._auth

    async def get(
        self, path: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """GET {root}{path}; ответ — объект JSON."""
        return await self._get(f"{self._root}{path.lstrip('/')}", params, path)

    async def pages(
        self, path: str, params: Mapping[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Элементы `value` всех страниц по `@odata.nextLink`."""
        url = f"{self._root}{path.lstrip('/')}"
        query: Mapping[str, Any] | None = params
        seen: set[str] = set()
        for _ in range(MAX_PAGES):
            page = await self._get(url, query, path)
            items = page.get("value")
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict):
                    yield item
            next_link = page.get("@odata.nextLink")
            if not isinstance(next_link, str) or not next_link:
                return
            if not next_link.startswith(self._root):
                # Ссылка на другой хост: токен туда не идёт.
                raise AdapterError("pagination_foreign")
            if next_link in seen:
                raise AdapterError("pagination_loop")
            seen.add(next_link)
            # Параметры уже внутри ссылки (skiptoken и исходные $select).
            url, query = next_link, None
        raise AdapterError("pagination_loop")

    async def download(self, path: str, *, max_bytes: int) -> bytes:
        url = f"{self._root}{path.lstrip('/')}"
        renewed = False
        backoff = iter(RATE_LIMIT_BACKOFF)
        while True:
            token = await self._auth.token(renew=renewed)
            try:
                downloaded = await self._http.download(
                    url,
                    max_bytes=max_bytes,
                    headers=_headers(token, accept="*/*"),
                    timeout=DOWNLOAD_TIMEOUT,
                )
            except OutboundTooLargeError as exc:
                raise AdapterError("document_too_large") from exc
            except httpx.TimeoutException as exc:
                raise AdapterError("timeout", retryable=True) from exc
            except httpx.HTTPError as exc:
                raise AdapterError("network_error", retryable=True) from exc
            status = downloaded.status_code
            if status == 200:
                return downloaded.content
            response = httpx.Response(
                status, headers=downloaded.headers, content=downloaded.content
            )
            if status == 401 and not renewed:
                renewed = True
                continue
            delay = _rate_delay(response, backoff)
            if delay is not None:
                await self._sleep(delay)
                continue
            raise _error(response)

    # --- внутреннее ----------------------------------------------------------------

    async def _get(
        self, url: str, params: Mapping[str, Any] | None, label: str
    ) -> dict[str, Any]:
        renewed = False
        backoff = iter(RATE_LIMIT_BACKOFF)
        while True:
            token = await self._auth.token(renew=renewed)
            try:
                response = await self._http.get(
                    url,
                    params=dict(params) if params else None,
                    headers=_headers(token, accept="application/json"),
                    timeout=REQUEST_TIMEOUT,
                    allow_redirects=False,
                )
            except OutboundTooLargeError as exc:
                raise AdapterError("response_too_large") from exc
            except httpx.TimeoutException as exc:
                raise AdapterError("timeout", retryable=True) from exc
            except httpx.HTTPError as exc:
                raise AdapterError("network_error", retryable=True) from exc
            if response.status_code == 401 and not renewed:
                renewed = True
                continue
            delay = _rate_delay(response, backoff)
            if delay is not None:
                await self._sleep(delay)
                continue
            data = json_object(response)
            if self._recorder is not None:
                self._recorder(
                    label,
                    redact(dict(params or {}), request=True),
                    redact(_without_download_urls(data)),
                )
            if response.status_code == 200 and data is not None:
                return data
            raise _error(response)


def _headers(token: str, *, accept: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "User-Agent": USER_AGENT,
    }


def _rate_delay(response: httpx.Response, backoff: Iterator[float]) -> float | None:
    """Пауза перед повтором при 429/503/504; None — не лимит. Паузы
    кончились — AdapterError (retryable)."""
    status = response.status_code
    if status not in _RETRY_STATUSES:
        return None
    delay = next(backoff, None)
    if delay is None:
        raise AdapterError(
            "rate_limited" if status == 429 else f"http_{status}", retryable=True
        )
    suggested = retry_after(response.headers.get("retry-after"))
    if suggested is not None:
        return min(suggested, MAX_RETRY_AFTER)
    return delay


def error_code(response: httpx.Response) -> str:
    """error.code из ответа Graph (для журнала и разбора), иначе пусто."""
    data = json_object(response) or {}
    error = data.get("error")
    if isinstance(error, dict) and error.get("code"):
        return str(error["code"])
    return ""


def _error(response: httpx.Response) -> AdapterError:
    status = response.status_code
    code = safe_code(error_code(response))
    if status == 401:
        return AdapterAuthError("unauthorized")
    if status == 403:
        # Нет согласия на нужное разрешение, сайт не выдан приложению
        # (Sites.Selected), условный доступ: наружу один код.
        logger.info("m365_forbidden", code=code)
        return AdapterError("forbidden")
    if status == 404:
        return AdapterError("not_found")
    if status == 410:
        return AdapterError("delta_resync", retryable=True)
    if status == 400:
        logger.info("m365_bad_request", code=code)
        return AdapterError("http_400")
    return AdapterError(f"http_{status}", retryable=status >= 500)


def _without_download_urls(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_download_urls(item)
            for key, item in value.items()
            if key != _DOWNLOAD_URL
        }
    if isinstance(value, list):
        return [_without_download_urls(item) for item in value]
    return value
