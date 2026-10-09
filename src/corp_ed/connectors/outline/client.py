"""RPC-клиент Outline (и совместимого Yonote) поверх OutboundClient.

По OpenAPI Outline (github.com/outline/openapi) и обработчикам сервера
(09.10): каждый метод — `POST {сайт}/api/<метод>` с JSON-телом,
`Authorization: Bearer <API-ключ>`. Списки — `offset`/`limit` (не больше
100) в теле, конец — короткая страница. Ошибки — `{ok: false, error,
message}`: в код идёт только `error` через safe_code, `message` (текст
сервера) не логируется.

Лимиты Outline — по ключу и методу: `documents.export` — 25 в минуту,
`documents.list` — 100 в минуту, прочие — общий лимит сервера. 429
приходит с Retry-After в целых секундах (до минуты): ждём столько,
сколько сказано (не больше MAX_RETRY_AFTER), до RATE_LIMIT_BACKOFF
попыток — экспорт после 25-го документа ждёт до сброса окна, а не
роняет запуск.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any

import httpx
import structlog

from corp_ed.connectors.base import AdapterAuthError, AdapterError
from corp_ed.connectors.common import Recorder, json_object, redact, safe_code
from corp_ed.core.outbound import OutboundClient, OutboundTooLargeError

logger = structlog.get_logger()

USER_AGENT = "corp-ed-connector/1.0"
REQUEST_TIMEOUT = 60.0
RATE_LIMIT_BACKOFF = (1.0, 2.0, 4.0, 8.0, 16.0)
MAX_RETRY_AFTER = 90.0
_RETRY_STATUSES = frozenset({429, 502, 503, 504})
_API_SUFFIX = "/api"

Sleep = Callable[[float], Awaitable[None]]


def site_root(address: str) -> str:
    """Адрес из настройки → корень сайта со слешем; хвост /api срезается."""
    root = address.strip().rstrip("/")
    if root.lower().endswith(_API_SUFFIX):
        root = root[: -len(_API_SUFFIX)]
    return root.rstrip("/") + "/"


class OutlineClient:
    def __init__(
        self,
        http: OutboundClient,
        *,
        base_url: str,
        token: str,
        sleep: Sleep = asyncio.sleep,
        recorder: Recorder | None = None,
    ) -> None:
        self._http = http
        self._root = site_root(base_url)
        self._token = token
        self._sleep = sleep
        self._recorder = recorder

    @property
    def root(self) -> str:
        return self._root

    async def call(
        self, method: str, body: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Вызов метода; ответ — объект целиком ({data, pagination})."""
        payload = dict(body or {})
        backoff = iter(RATE_LIMIT_BACKOFF)
        while True:
            try:
                response = await self._http.post(
                    f"{self._root}api/{method}",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "User-Agent": USER_AGENT,
                    },
                    timeout=REQUEST_TIMEOUT,
                    allow_redirects=False,
                )
            except OutboundTooLargeError as exc:
                raise AdapterError("response_too_large") from exc
            except httpx.TimeoutException as exc:
                raise AdapterError("timeout", retryable=True) from exc
            except httpx.HTTPError as exc:
                raise AdapterError("network_error", retryable=True) from exc
            status = response.status_code
            if status in _RETRY_STATUSES:
                delay = next(backoff, None)
                if delay is None:
                    code = "rate_limited" if status == 429 else f"http_{status}"
                    raise AdapterError(code, retryable=True)
                await self._sleep(_retry_after(response, delay))
                continue
            data = json_object(response)
            if self._recorder is not None:
                self._recorder(method, redact(payload, request=True), redact(data))
            if status == 200:
                if data is None:
                    # HTML вместо JSON — страница входа или не тот адрес.
                    raise AdapterError("not_json")
                return data
            error = str((data or {}).get("error") or "")
            if status == 401:
                raise AdapterAuthError("unauthorized")
            if status == 403:
                logger.info("outline_forbidden", method=method, error=safe_code(error))
                raise AdapterError("forbidden")
            if status == 404:
                raise AdapterError("not_found")
            if error:
                raise AdapterError(
                    safe_code(error, prefix="outline_"), retryable=status >= 500
                )
            raise AdapterError(f"http_{status}", retryable=status >= 500)

    async def pages(
        self,
        method: str,
        body: Mapping[str, Any] | None = None,
        *,
        key: str | None = None,
        limit: int = 100,
    ) -> AsyncIterator[dict[str, Any]]:
        """Постраничный обход: элементы data (key=None) или data[key].

        Для ответов вида {data: {memberships: [...], users: [...]}} key —
        основной список, по длине которого виден конец.
        """
        offset = 0
        while True:
            page = await self.call(
                method, {**(body or {}), "offset": offset, "limit": limit}
            )
            data = page.get("data")
            if key is not None:
                data = data.get(key) if isinstance(data, dict) else None
            items = data if isinstance(data, list) else []
            for item in items:
                if isinstance(item, dict):
                    yield item
            if len(items) < limit:
                return
            offset += len(items)


def _retry_after(response: httpx.Response, default: float) -> float:
    value = response.headers.get("retry-after", "").strip()
    if value.isdigit() and int(value) > 0:
        return min(float(value), MAX_RETRY_AFTER)
    return default
