"""HTTP-клиент Drive API v3 и Directory API поверх OutboundClient.

Каждый вызов — от имени конкретного сотрудника домена (subject) и с
токеном на один scope (auth.py). Списки — pageToken / nextPageToken.
Ошибки Google — {error: {code, message, errors: [{reason}]}}: наружу
идёт только код, текст (в нём почты и имена файлов) — никуда.

Лимиты (developers.google.com/workspace/drive/api/guides/limits): 429
или 403 с причиной rateLimitExceeded / userRateLimitExceeded —
экспоненциальная пауза (с Retry-After, если пришёл), после последней —
AdapterError('rate_limited', retryable). 401 — токен обновляется один
раз, второй 401 — AdapterAuthError. 403 exportSizeLimitExceeded —
документ Google больше лимита экспорта (10 МБ) — document_too_large;
прочие 403 — forbidden; 404 — not_found; 5xx — повтор задачи позже.

Содержимое файла (alt=media) Google отдаёт редиректом на
*.googleusercontent.com — OutboundClient проверяет новый адрес и не
несёт туда Authorization (ссылка подписана сама).
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any

import httpx
import structlog

from corp_ed.connectors.base import AdapterAuthError, AdapterError
from corp_ed.connectors.common import Recorder, json_object, redact, safe_code
from corp_ed.connectors.gdrive.auth import DRIVE_SCOPE, ServiceAccountAuth
from corp_ed.core.outbound import OutboundClient, OutboundTooLargeError

logger = structlog.get_logger()

USER_AGENT = "corp-ed-connector/1.0"
REQUEST_TIMEOUT = 30.0
DOWNLOAD_TIMEOUT = 120.0
RATE_LIMIT_BACKOFF = (1.0, 2.0, 4.0, 8.0, 16.0)
MAX_RETRY_AFTER = 30.0
_RATE_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
_TOO_LARGE_REASONS = frozenset({"exportSizeLimitExceeded"})

Sleep = Callable[[float], Awaitable[None]]


class GoogleClient:
    def __init__(
        self,
        http: OutboundClient,
        auth: ServiceAccountAuth,
        *,
        drive_api: str,
        directory_api: str,
        sleep: Sleep = asyncio.sleep,
        recorder: Recorder | None = None,
    ) -> None:
        self._http = http
        self._auth = auth
        self._drive = drive_api if drive_api.endswith("/") else drive_api + "/"
        self._directory = (
            directory_api if directory_api.endswith("/") else directory_api + "/"
        )
        self._sleep = sleep
        self._recorder = recorder

    @property
    def auth(self) -> ServiceAccountAuth:
        return self._auth

    async def drive(
        self, subject: str, path: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """GET {drive_api}{path} от имени subject."""
        return await self._get(self._drive, subject, DRIVE_SCOPE, path, params)

    async def directory(
        self,
        subject: str,
        scope: str,
        path: str,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET {directory_api}{path} от имени администратора."""
        return await self._get(self._directory, subject, scope, path, params)

    async def drive_pages(
        self, subject: str, path: str, params: Mapping[str, Any], key: str
    ) -> AsyncIterator[dict[str, Any]]:
        async for item in self._pages(self.drive, subject, path, params, key):
            yield item

    async def directory_pages(
        self, subject: str, scope: str, path: str, params: Mapping[str, Any], key: str
    ) -> AsyncIterator[dict[str, Any]]:
        async def call(
            subject: str, path: str, params: Mapping[str, Any]
        ) -> dict[str, Any]:
            return await self.directory(subject, scope, path, params)

        async for item in self._pages(call, subject, path, params, key):
            yield item

    async def download(
        self,
        subject: str,
        path: str,
        params: Mapping[str, str],
        *,
        max_bytes: int,
    ) -> bytes:
        url = str(httpx.URL(f"{self._drive}{path}", params=dict(params)))
        renewed = False
        backoff = iter(RATE_LIMIT_BACKOFF)
        while True:
            token = await self._auth.token(subject, DRIVE_SCOPE, renew=renewed)
            try:
                downloaded = await self._http.download(
                    url,
                    max_bytes=max_bytes,
                    headers=self._headers(token, accept="*/*"),
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
            delay = self._rate_delay(response, backoff)
            if delay is not None:
                await self._sleep(delay)
                continue
            raise _error(response)

    # --- внутреннее ----------------------------------------------------------------

    async def _pages(
        self,
        call: Callable[[str, str, Mapping[str, Any]], Awaitable[dict[str, Any]]],
        subject: str,
        path: str,
        params: Mapping[str, Any],
        key: str,
    ) -> AsyncIterator[dict[str, Any]]:
        token: str | None = None
        seen: set[str] = set()
        while True:
            page = await call(
                subject, path, {**params, **({"pageToken": token} if token else {})}
            )
            items = page.get(key)
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict):
                    yield item
            next_token = page.get("nextPageToken")
            if not isinstance(next_token, str) or not next_token:
                return
            if next_token in seen:
                # Сервер вернул уже пройденную страницу: дальше — по кругу.
                raise AdapterError("pagination_loop")
            seen.add(next_token)
            token = next_token

    async def _get(
        self,
        base: str,
        subject: str,
        scope: str,
        path: str,
        params: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        url = f"{base}{path.lstrip('/')}"
        renewed = False
        backoff = iter(RATE_LIMIT_BACKOFF)
        while True:
            token = await self._auth.token(subject, scope, renew=renewed)
            try:
                response = await self._http.get(
                    url,
                    params=dict(params or {}),
                    headers=self._headers(token, accept="application/json"),
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
            delay = self._rate_delay(response, backoff)
            if delay is not None:
                await self._sleep(delay)
                continue
            data = json_object(response)
            if self._recorder is not None:
                self._recorder(
                    path, redact(dict(params or {}), request=True), redact(data)
                )
            if response.status_code == 200 and data is not None:
                return data
            raise _error(response)

    def _rate_delay(self, response: httpx.Response, backoff: Any) -> float | None:
        """Пауза перед повтором, если это лимит квоты; None — не лимит.
        Паузы кончились — AdapterError('rate_limited')."""
        status = response.status_code
        if status != 429 and not (status == 403 and _reason(response) in _RATE_REASONS):
            return None
        delay = next(backoff, None)
        if delay is None:
            raise AdapterError("rate_limited", retryable=True)
        value = response.headers.get("retry-after", "")
        if value.isdigit():
            return min(float(value), MAX_RETRY_AFTER)
        return float(delay)

    @staticmethod
    def _headers(token: str, *, accept: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": accept,
            "User-Agent": USER_AGENT,
        }


def _reason(response: httpx.Response) -> str:
    data = json_object(response) or {}
    error = data.get("error")
    if not isinstance(error, dict):
        return ""
    errors = error.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return str(errors[0].get("reason") or "")
    return str(error.get("status") or "")


def _error(response: httpx.Response) -> AdapterError:
    status = response.status_code
    reason = _reason(response)
    if status == 401:
        return AdapterAuthError("unauthorized")
    if status == 403 and reason in _TOO_LARGE_REASONS:
        return AdapterError("document_too_large")
    if status == 403:
        # Причин у 403 много (нет прав, скачивание запрещено владельцем,
        # домен запрещает): наружу один код, причина — в журнал.
        logger.info("gdrive_forbidden", reason=safe_code(reason))
        return AdapterError("forbidden")
    if status == 404:
        return AdapterError("not_found")
    if status == 400:
        logger.info("gdrive_bad_request", reason=safe_code(reason))
        return AdapterError("http_400")
    return AdapterError(f"http_{status}", retryable=status >= 500)
