"""HTTP публичного сайта поверх OutboundClient: вежливо и без учётных данных.

- свой User-Agent с адресом нашего сайта — админ сайта видит, кто ходит,
  и может ограничить нас в robots.txt по имени kronto-bot;
- пауза между запросами: не меньше MIN_INTERVAL, а если robots.txt задаёт
  Crawl-delay — его (не больше MAX_CRAWL_DELAY, иначе один запуск не
  обошёл бы и десятка страниц); fast — без пауз (тесты, connector-check);
- 429 и 503 — ожидание по Retry-After (секунды или дата, не дольше
  common.MAX_RETRY_AFTER) или нарастающая пауза; 502/504 — нарастающая пауза;
  попытки кончились — AdapterError, retryable: задачу повторит воркер;
- сеть и таймауты — коды timeout / network_error, тела ответов не
  попадают ни в коды, ни в журнал.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterator

import httpx

from corp_ed.connectors.base import AdapterError
from corp_ed.connectors.common import retry_after
from corp_ed.core.outbound import Downloaded, OutboundClient, OutboundTooLargeError

USER_AGENT = "Mozilla/5.0 (compatible; kronto-bot/1.0; +https://krontoai.ru/)"
ROBOTS_AGENT = "kronto-bot"
MIN_INTERVAL = 1.0
MAX_CRAWL_DELAY = 10.0
RETRY_BACKOFF = (2.0, 5.0, 10.0)
REQUEST_TIMEOUT = 30.0
DOWNLOAD_TIMEOUT = 120.0
_RATE_LIMITED = frozenset({429, 503})
_GATEWAY = frozenset({502, 504})

Sleep = Callable[[float], Awaitable[None]]


class SiteClient:
    def __init__(
        self,
        http: OutboundClient,
        *,
        min_interval: float = MIN_INTERVAL,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._interval = min_interval
        self._paced = min_interval > 0
        self._sleep = sleep
        self._clock = clock
        self._lock = asyncio.Lock()
        self._next_slot = 0.0

    def apply_crawl_delay(self, delay: float | None) -> None:
        """Crawl-delay из robots.txt: пауза не короче его (с потолком)."""
        if self._paced and delay is not None:
            self._interval = max(self._interval, min(delay, MAX_CRAWL_DELAY))

    async def get(
        self, url: str, *, max_bytes: int, follow: bool = False
    ) -> httpx.Response:
        """GET страницы или служебного файла. follow=False — редирект
        возвращается как есть: куда он ведёт, решает обход (тот же сайт и
        раздел или нет)."""
        return await self._send("GET", url, max_bytes=max_bytes, follow=follow)

    async def head(self, url: str, *, follow: bool = False) -> httpx.Response:
        return await self._send("HEAD", url, max_bytes=0, follow=follow)

    async def download(self, url: str, *, max_bytes: int) -> Downloaded:
        backoff = iter(RETRY_BACKOFF)
        while True:
            await self._pace()
            try:
                downloaded = await self._http.download(
                    url,
                    max_bytes=max_bytes,
                    headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
                    timeout=DOWNLOAD_TIMEOUT,
                )
            except OutboundTooLargeError as exc:
                raise AdapterError("document_too_large") from exc
            except httpx.TimeoutException as exc:
                raise AdapterError("timeout", retryable=True) from exc
            except httpx.HTTPError as exc:
                raise AdapterError("network_error", retryable=True) from exc
            status = downloaded.status_code
            if status in _RATE_LIMITED or status in _GATEWAY:
                await self._wait(status, downloaded.headers, backoff)
                continue
            return downloaded

    async def _send(
        self, method: str, url: str, *, max_bytes: int, follow: bool
    ) -> httpx.Response:
        backoff = iter(RETRY_BACKOFF)
        while True:
            await self._pace()
            try:
                response = await self._http.request(
                    method,
                    url,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "text/html,application/xhtml+xml,application/xml;"
                        "q=0.9,*/*;q=0.8",
                        "Accept-Language": "ru,en;q=0.8",
                    },
                    timeout=REQUEST_TIMEOUT,
                    allow_redirects=follow,
                    max_bytes=max_bytes,
                )
            except httpx.TimeoutException as exc:
                raise AdapterError("timeout", retryable=True) from exc
            except httpx.HTTPError as exc:
                raise AdapterError("network_error", retryable=True) from exc
            status = response.status_code
            if status in _RATE_LIMITED or status in _GATEWAY:
                await self._wait(status, response.headers, backoff)
                continue
            return response

    async def _wait(
        self, status: int, headers: httpx.Headers, backoff: Iterator[float]
    ) -> None:
        delay = next(backoff, None)
        if delay is None:
            if status in _RATE_LIMITED:
                raise AdapterError("rate_limited", retryable=True)
            raise AdapterError(f"http_{status}", retryable=True)
        if status in _RATE_LIMITED:
            delay = retry_after(headers.get("retry-after")) or delay
        await self._sleep(delay)

    async def _pace(self) -> None:
        if not self._paced:
            return
        async with self._lock:
            now = self._clock()
            wait = self._next_slot - now
            if wait > 0:
                await self._sleep(wait)
                now = self._clock()
            self._next_slot = max(now, self._next_slot) + self._interval
