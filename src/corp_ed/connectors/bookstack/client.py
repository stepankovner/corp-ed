"""REST-клиент BookStack поверх OutboundClient.

По встроенной документации API (`/api/docs`) и коду BookStack v26.09
(app/Api, app/Http/Middleware/ThrottleApiRequests.php): каждый вызов —
`GET {сайт}/api/<путь>` с заголовком `Authorization: Token <id>:<secret>`.
Списки — `count`/`offset` (по умолчанию 100, не больше 500 — настройка
сервера API_MAX_ITEM_COUNT) и `{data, total}` в ответе; порядок —
`sort=id`, чтобы страницы не съезжали между запросами. Ошибки —
`{error: {code, message}}`: текст сервера не логируется.

Лимит API — 180 запросов в минуту на пользователя (API_REQUESTS_PER_MIN);
сверх — 429 с Retry-After в секундах до сброса окна (не больше минуты):
ждём столько, сколько сказано (не больше MAX_RETRY_AFTER), до
RATE_LIMIT_BACKOFF попыток.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any

import httpx
import structlog

from corp_ed.connectors.base import AdapterAuthError, AdapterError
from corp_ed.connectors.common import Recorder, json_object, redact, to_int
from corp_ed.core.outbound import OutboundClient, OutboundTooLargeError

logger = structlog.get_logger()

USER_AGENT = "corp-ed-connector/1.0"
REQUEST_TIMEOUT = 60.0
PAGE_COUNT = 100
RATE_LIMIT_BACKOFF = (1.0, 2.0, 4.0, 8.0, 16.0)
MAX_RETRY_AFTER = 60.0
_RETRY_STATUSES = frozenset({429, 502, 503, 504})
_API_SUFFIX = "/api"
_RECORDED_CONTENT = 256
"""Сколько символов `content` вложения (base64 файла) попадает в запись."""

Sleep = Callable[[float], Awaitable[None]]


def site_root(address: str) -> str:
    """Адрес из настройки → корень сайта со слешем; хвост /api срезается.
    BookStack может жить и в подпапке (`https://company.ru/wiki/`)."""
    root = address.strip().rstrip("/")
    if root.lower().endswith(_API_SUFFIX):
        root = root[: -len(_API_SUFFIX)]
    return root.rstrip("/") + "/"


class BookStackClient:
    def __init__(
        self,
        http: OutboundClient,
        *,
        base_url: str,
        token_id: str,
        token_secret: str,
        sleep: Sleep = asyncio.sleep,
        recorder: Recorder | None = None,
    ) -> None:
        self._http = http
        self._root = site_root(base_url)
        self._authorization = f"Token {token_id}:{token_secret}"
        self._sleep = sleep
        self._recorder = recorder

    @property
    def root(self) -> str:
        return self._root

    async def get(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        """GET /api/{path} → объект JSON. max_bytes — предел ответа
        (вложение приходит base64 внутри JSON); больше —
        AdapterError('response_too_large')."""
        query = dict(params or {})
        backoff = iter(RATE_LIMIT_BACKOFF)
        while True:
            try:
                response = await self._http.get(
                    f"{self._root}api/{path.lstrip('/')}",
                    params=query,
                    headers={
                        "Authorization": self._authorization,
                        "Accept": "application/json",
                        "User-Agent": USER_AGENT,
                    },
                    timeout=REQUEST_TIMEOUT,
                    allow_redirects=False,
                    max_bytes=max_bytes,
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
                self._recorder(
                    path, redact(query, request=True), redact(_for_record(data))
                )
            if status == 200:
                if data is None:
                    # HTML вместо JSON — страница входа или не тот адрес.
                    raise AdapterError("not_json")
                return data
            if status == 401:
                raise AdapterAuthError("unauthorized")
            if status == 403:
                # Токен истёк, у роли нет «Access System API» или нет права
                # на сам запрос — что именно, решает вызывающий по месту.
                logger.info("bookstack_forbidden", path=path.split("/", 1)[0])
                raise AdapterError("forbidden")
            if status == 404:
                raise AdapterError("not_found")
            raise AdapterError(f"http_{status}", retryable=status >= 500)

    async def pages(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        count: int = PAGE_COUNT,
    ) -> AsyncIterator[dict[str, Any]]:
        """Постраничный обход списка: элементы `data`. Конец — короткая
        или пустая страница либо offset дошёл до `total`."""
        offset = 0
        while True:
            page = await self.get(
                path, {**(params or {}), "count": count, "offset": offset, "sort": "id"}
            )
            data = page.get("data")
            items = data if isinstance(data, list) else []
            for item in items:
                if isinstance(item, dict):
                    yield item
            offset += len(items)
            total = to_int(page.get("total"))
            if len(items) < count or (total is not None and offset >= total):
                return


def _retry_after(response: httpx.Response, default: float) -> float:
    value = response.headers.get("retry-after", "").strip()
    if value.isdigit() and int(value) > 0:
        return min(float(value), MAX_RETRY_AFTER)
    return default


def _for_record(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """Файл вложения (base64 в `content`) в записи не нужен — только начало."""
    if data is None or not isinstance(data.get("content"), str):
        return data
    content = data["content"]
    if len(content) <= _RECORDED_CONTENT:
        return data
    return {**data, "content": content[:_RECORDED_CONTENT] + "…"}
