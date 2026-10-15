"""HTTP-клиент amoCRM поверх OutboundClient: API аккаунта, сервис файлов,
скачивание, темп и продление токена.

Два хоста: аккаунт (`https://<поддомен>.amocrm.ru/api/v4/…`) и сервис
файлов (`drive_url` из `/api/v4/account?with=drive_url`, например
`https://drive-b.amocrm.ru/v1.0/…`). Токен сотрудника (Bearer) уходит
только на них; адрес сервиса файлов пришёл из ответа — он проверяется
так же, как адрес аккаунта (поддомен той же платформы).

Лимиты (developers/content/api/recommendations, 15.10.2026): не больше
7 запросов в секунду на интеграцию — общий на всех сотрудников —
и 50 на аккаунт; сверх — 429, а повторяющиеся превышения блокируют
аккаунт: дальше 403 на любой запрос. Поэтому темп — с запасом
(MIN_INTERVAL), на 429 — две паузы и ошибка retryable, без долгих
повторов. 402 — аккаунт не оплачен (чтение отключается через 30 дней).

Пустой список amoCRM отдаёт кодом 204 без тела — это None, а не ошибка.
Редиректы API не следуются: смена адреса аккаунта — код account_moved.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from corp_ed.connectors.amocrm.oauth import (
    AmoOAuth,
    account_root,
    platform_of,
    service_host_allowed,
)
from corp_ed.connectors.base import AdapterAuthError, AdapterConfigError, AdapterError
from corp_ed.connectors.common import Recorder, TokenSet, json_object, redact
from corp_ed.core.outbound import OutboundClient, OutboundTooLargeError

USER_AGENT = "corp-ed-connector/1.0"
REQUEST_TIMEOUT = 30.0
DOWNLOAD_TIMEOUT = 120.0
MIN_INTERVAL = 0.25
"""Секунд между запросами: 4 в секунду при пределе 7 на интеграцию."""
RATE_LIMIT_BACKOFF = (2.0, 5.0)
# Продлить заранее, если access_token истекает раньше: обход одного
# сотрудника не должен оборваться 401 на середине.
REFRESH_AHEAD = 10 * 60
_RETRY_STATUSES = frozenset({429, 502, 503, 504})

Sleep = Callable[[float], Awaitable[None]]


@dataclass
class AmoAuth:
    """Токены сотрудника. oauth=None — долгосрочный токен без refresh
    (только для cli connector-check): продлевать нечем."""

    tokens: TokenSet
    oauth: AmoOAuth | None
    refreshed: bool = False
    clock: Callable[[], float] = time.time
    _checked: bool = field(default=False, repr=False)

    async def header(self) -> str:
        if not self._checked:
            self._checked = True
            expires_at = self.tokens.expires_at
            if (
                self.tokens.refresh_token
                and expires_at
                and expires_at - self.clock() < REFRESH_AHEAD
            ):
                await self.refresh()
        return f"Bearer {self.tokens.access_token}"

    async def refresh(self) -> None:
        if not self.tokens.refresh_token or self.oauth is None:
            raise AdapterAuthError("refresh_token_missing")
        self.tokens = await self.oauth.refresh(self.tokens.refresh_token)
        self.refreshed = True

    @property
    def refreshed_credentials(self) -> Mapping[str, str] | None:
        return self.tokens.as_credentials() if self.refreshed else None


class AmoClient:
    def __init__(
        self,
        http: OutboundClient,
        *,
        account: str,
        auth: AmoAuth,
        min_interval: float = MIN_INTERVAL,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        recorder: Recorder | None = None,
    ) -> None:
        root = account_root(account)
        if root is None:
            raise AdapterConfigError("account_invalid")
        self._http = http
        self._account = root
        self._host = urlsplit(root).hostname or ""
        self._platform = platform_of(self._host) or ""
        self._auth = auth
        self._drive: str | None = None
        self._min_interval = min_interval
        self._sleep = sleep
        self._clock = clock
        self._recorder = recorder
        self._lock = asyncio.Lock()
        self._next_slot = 0.0

    @property
    def account(self) -> str:
        """Корень аккаунта со слешем: https://<поддомен>.amocrm.ru/."""
        return self._account

    @property
    def subdomain(self) -> str:
        return self._host.split(".", 1)[0]

    @property
    def refreshed_credentials(self) -> Mapping[str, str] | None:
        return self._auth.refreshed_credentials

    def set_drive(self, url: str) -> None:
        """Адрес сервиса файлов из ответа аккаунта — после проверки хоста."""
        if not service_host_allowed(url, platform=self._platform):
            raise AdapterConfigError("drive_url_invalid")
        host = (urlsplit(url).hostname or "").lower()
        self._drive = f"https://{host}/"

    # --- вызовы -----------------------------------------------------------------

    async def api(
        self, path: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """GET /api/v4/{path} аккаунта → объект JSON; 204 — None.

        403 — AdapterError("forbidden"), 404 — ("not_found"): что они
        значат (нет прав на сделку или заблокирован аккаунт), решает
        вызывающий по месту.
        """
        return await self._get(f"{self._account}api/v4/{path}", path, params)

    async def drive(
        self, path: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """GET {drive_url}/v1.0/{path}. Сервис файлов ограничен scope
        интеграции («Доступ к файлам»), а не правами пользователя: 403 —
        у интеграции нет этого доступа, вопрос к админу."""
        if self._drive is None:
            raise AdapterError("drive_url_unknown")
        try:
            return await self._get(f"{self._drive}v1.0/{path}", f"drive:{path}", params)
        except AdapterError as exc:
            if exc.code == "forbidden":
                raise AdapterConfigError("files_scope_missing") from exc
            raise

    async def download(self, url: str, *, max_bytes: int) -> bytes:
        """Файл по ссылке из ответа сервиса файлов: только https на его
        хосте. Редирект на хранилище уходит без токена (OutboundClient
        снимает заголовки авторизации при смене хоста)."""
        drive_host = urlsplit(self._drive or "").hostname
        if (
            drive_host is None
            or not service_host_allowed(url, platform=self._platform)
            or (urlsplit(url).hostname or "").lower() != drive_host
        ):
            raise AdapterError("download_url_foreign")
        await self._pace()
        try:
            downloaded = await self._http.download(
                url,
                max_bytes=max_bytes,
                headers={
                    "Authorization": await self._auth.header(),
                    "User-Agent": USER_AGENT,
                    "Accept": "*/*",
                },
                timeout=DOWNLOAD_TIMEOUT,
            )
        except OutboundTooLargeError as exc:
            raise AdapterError("document_too_large") from exc
        except httpx.TimeoutException as exc:
            raise AdapterError("timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise AdapterError("network_error", retryable=True) from exc
        status = downloaded.status_code
        if status != 200:
            raise AdapterError(
                f"download_http_{status}", retryable=status >= 500 or status == 429
            )
        if downloaded.headers.get("content-type", "").lower().startswith("text/html"):
            # Страница входа или ошибки вместо файла.
            raise AdapterError("download_failed")
        return downloaded.content

    # --- внутреннее ---------------------------------------------------------------

    async def _get(
        self, url: str, label: str, params: Mapping[str, Any] | None
    ) -> dict[str, Any] | None:
        refreshed = False
        backoff = iter(RATE_LIMIT_BACKOFF)
        while True:
            response = await self._send(url, params)
            status = response.status_code
            if status in _RETRY_STATUSES:
                delay = next(backoff, None)
                if delay is None:
                    code = "rate_limited" if status == 429 else f"http_{status}"
                    raise AdapterError(code, retryable=True)
                await self._sleep(delay)
                continue
            if status == 401 and not refreshed and self._auth.oauth is not None:
                # access_token истёк раньше срока или отозван: одно продление.
                await self._auth.refresh()
                refreshed = True
                continue
            data = json_object(response)
            self._record(label, params, data if data is not None else status)
            if status == 204:
                return None
            if status == 200:
                if data is None:
                    # HTML вместо JSON — страница входа или не тот адрес.
                    raise AdapterError("not_json")
                return data
            if response.is_redirect:
                raise AdapterError("account_moved")
            if status == 401:
                raise AdapterAuthError("unauthorized")
            if status == 402:
                raise AdapterConfigError("account_unpaid")
            if status == 403:
                raise AdapterError("forbidden")
            if status == 404:
                raise AdapterError("not_found")
            raise AdapterError(f"http_{status}", retryable=status >= 500)

    async def _send(self, url: str, params: Mapping[str, Any] | None) -> httpx.Response:
        await self._pace()
        try:
            return await self._http.get(
                url,
                params=dict(params or {}),
                headers={
                    "Authorization": await self._auth.header(),
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

    async def _pace(self) -> None:
        async with self._lock:
            now = self._clock()
            wait = self._next_slot - now
            if wait > 0:
                await self._sleep(wait)
                now = self._clock()
            self._next_slot = max(now, self._next_slot) + self._min_interval

    def _record(self, label: str, params: Mapping[str, Any] | None, data: Any) -> None:
        if self._recorder is not None:
            self._recorder(
                label,
                redact(dict(params or {}), request=True),
                _without_download_links(redact(data)),
            )


def _without_download_links(value: Any) -> Any:
    """Ссылки на скачивание из ответов сервиса файлов — не в фикстуры:
    что значит первый сегмент пути /download/{…}/{uuid}/…, документация
    не говорит, и это может быть подпись."""
    if isinstance(value, dict):
        return {
            key: (
                "<redacted>"
                if isinstance(item, str) and "/download/" in item
                else _without_download_links(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_without_download_links(item) for item in value]
    return value
