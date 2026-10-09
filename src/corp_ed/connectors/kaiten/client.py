"""HTTP-клиент REST Kaiten поверх OutboundClient.

По developers.kaiten.ru (09.10): адрес `https://<домен>.kaiten.ru/api/latest`
(у коробки — свой домен компании), заголовок `Authorization: Bearer
<токен из профиля>` (`/profile/api-key`), JSON. Лимит — 50 запросов в
секунду; сверх него 429 и заголовки `X-RateLimit-Remaining` /
`X-RateLimit-Reset` (эпоха UTC, когда лимит обнулится). Ожидание — до
этого момента (не больше MAX_RETRY_AFTER), без заголовка — по
RATE_LIMIT_BACKOFF; 502–504 повторяются так же.

Ответы об ошибках: 401 — строка «Invalid token», 403 и 404 — без тела.
Тело ответа в коды и логи не попадает. Файлы карточек скачиваются по
временной подписанной ссылке без токена: документация прямо запрещает
отправлять его по такой ссылке.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from corp_ed.connectors.base import AdapterAuthError, AdapterError
from corp_ed.connectors.common import Recorder, redact
from corp_ed.core.outbound import OutboundClient, OutboundTooLargeError

USER_AGENT = "corp-ed-connector/1.0"
REQUEST_TIMEOUT = 30.0
DOWNLOAD_TIMEOUT = 120.0
RATE_LIMIT_BACKOFF = (0.5, 1.0, 2.0, 4.0)
MAX_RETRY_AFTER = 30.0
_RETRY_STATUSES = frozenset({429, 502, 503, 504})
# Хвосты адреса API, которые админ мог вставить вместе с адресом.
_API_SUFFIXES = ("/api/latest", "/api/v1", "/api")

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


def site_root(address: str) -> str:
    """Адрес из настройки → корень сайта со слешем на конце."""
    root = address.strip().rstrip("/")
    for suffix in _API_SUFFIXES:
        if root.lower().endswith(suffix):
            root = root[: -len(suffix)]
            break
    return root.rstrip("/") + "/"


class KaitenClient:
    def __init__(
        self,
        http: OutboundClient,
        *,
        base_url: str,
        token: str,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.time,
        recorder: Recorder | None = None,
    ) -> None:
        self._http = http
        self._root = site_root(base_url)
        self._api = f"{self._root}api/latest/"
        self._token = token
        self._sleep = sleep
        self._clock = clock
        self._recorder = recorder

    @property
    def root(self) -> str:
        """Корень сайта Kaiten (для ссылок на документы и карточки)."""
        return self._root

    async def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """GET /api/latest/{path} → разобранный JSON (объект или список)."""
        backoff = iter(RATE_LIMIT_BACKOFF)
        while True:
            try:
                response = await self._http.get(
                    f"{self._api}{path.lstrip('/')}",
                    params=dict(params or {}),
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Accept": "application/json",
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
                await self._sleep(self._wait(response, delay))
                continue
            try:
                data: Any = response.json()
            except ValueError:
                data = None
            if self._recorder is not None:
                self._recorder(
                    path, redact(dict(params or {}), request=True), redact(data)
                )
            if status == 200:
                if data is None:
                    # HTML вместо JSON — страница входа или не тот адрес.
                    raise AdapterError("not_json")
                return data
            if status == 401:
                raise AdapterAuthError("unauthorized")
            if status == 403:
                raise AdapterError("forbidden")
            if status == 404:
                raise AdapterError("not_found")
            if status == 422:
                # Файл помечен как вредоносный (restricted-access-card-files).
                raise AdapterError("file_rejected")
            raise AdapterError(f"http_{status}", retryable=status >= 500)

    async def get_object(
        self, path: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        data = await self.get(path, params)
        if not isinstance(data, dict):
            raise AdapterError("unexpected_response")
        return data

    async def get_list(
        self, path: str, params: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        data = await self.get(path, params)
        if not isinstance(data, list):
            raise AdapterError("unexpected_response")
        return [item for item in data if isinstance(item, dict)]

    async def download(self, link: str, *, max_bytes: int) -> bytes:
        """Файл по ссылке из ответа API — без токена сотрудника."""
        url = urljoin(self._root, link)
        if urlsplit(url).scheme != "https":
            raise AdapterError("link_invalid")
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
        if downloaded.status_code != 200:
            raise AdapterError(
                f"download_http_{downloaded.status_code}",
                retryable=downloaded.status_code >= 500,
            )
        if downloaded.headers.get("content-type", "").lower().startswith("text/html"):
            # Страница входа или ошибки вместо файла: ссылка истекла.
            raise AdapterError("download_failed")
        return downloaded.content

    def _wait(self, response: httpx.Response, default: float) -> float:
        """Пауза до сброса лимита: X-RateLimit-Reset (эпоха) или Retry-After."""
        reset = response.headers.get("x-ratelimit-reset", "")
        try:
            delay = float(reset) - self._clock()
        except ValueError:
            retry_after = response.headers.get("retry-after", "")
            delay = float(retry_after) if retry_after.isdigit() else 0.0
        if delay <= 0:
            return default
        return min(delay, MAX_RETRY_AFTER)
