"""REST-клиент Битрикс24 поверх OutboundClient.

Что здесь, а не в модулях: адрес вызова для вебхука и для OAuth-токена,
темп запросов, постраничный обход по `next`, разбор ошибок по коду из
поля `error` (не по тексту — он локализован и меняется), продление
access_token по `expired_token` с одним повтором, скачивание файла по
подписанной ссылке только с хоста портала.

Редиректы не следуются: 301/302 от портала — смена его адреса, и
повтор POST как GET потерял бы тело (документация «Особенности вызовов
REST при изменении адреса»); коннектор получает код portal_moved, а
админ обновляет адрес в настройках.

Лимиты облака: устойчиво 2 запроса/с (тарифы кроме «Энтерпрайз»),
превышение — 503 QUERY_LIMIT_EXCEEDED. Клиент держит паузу между
запросами и при 503 ждёт с нарастающей задержкой; исчерпал попытки —
ошибка retryable, задачу повторит воркер.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from corp_ed.connectors.base import AdapterAuthError, AdapterConfigError, AdapterError
from corp_ed.connectors.bitrix24.oauth import Bitrix24OAuth, TokenSet
from corp_ed.core.outbound import OutboundClient, OutboundTooLargeError

USER_AGENT = "corp-ed-connector/1.0"
PAGE_SIZE = 50
MIN_INTERVAL = 0.5
"""Секунд между запросами: 2 запроса/с — устойчивая интенсивность тарифов."""
RATE_LIMIT_BACKOFF = (1.0, 2.0, 4.0)
REQUEST_TIMEOUT = 30.0
DOWNLOAD_TIMEOUT = 120.0
# Заголовки для подписанной ссылки на скачивание — из документации
# («Заголовки запроса»): без User-Agent и Referer nginx портала отдаёт
# 404 вместо файла.
_DOWNLOAD_ACCEPT_LANGUAGE = "ru-RU,ru;q=0.9,en;q=0.8"

# Системные коды REST (error-codes.md) → чья это проблема.
_AUTH_CODES = frozenset(
    {
        "expired_token",
        "invalid_token",
        "no_auth_found",
        "invalid_credentials",
        "user_access_error",
    }
)
_CONFIG_CODES = frozenset(
    {"insufficient_scope", "error_method_not_found", "wrong_auth_type"}
)
_RETRYABLE_CODES = frozenset(
    {
        "query_limit_exceeded",
        "operation_time_limit",
        "internal_server_error",
        "error_unexpected_answer",
    }
)
_FATAL_CODES = frozenset({"portal_deleted", "overload_limit"})

Sleep = Callable[[float], Awaitable[None]]
Recorder = Callable[[str, Mapping[str, Any], Any], None]
"""(method, params без auth, ответ с вырезанными токенами) — для записи
контрактных фикстур из cli connector-check --record."""


@dataclass(frozen=True)
class WebhookAuth:
    """Входящий вебхук: код в адресе, портал — из него же. Только для
    cli connector-check и записи фикстур: в продукте — OAuth."""

    url: str


@dataclass
class OAuthAuth:
    tokens: TokenSet
    oauth: Bitrix24OAuth
    refreshed: bool = False


class Bitrix24Client:
    def __init__(
        self,
        http: OutboundClient,
        *,
        portal: str,
        auth: WebhookAuth | OAuthAuth,
        min_interval: float = MIN_INTERVAL,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        recorder: Recorder | None = None,
    ) -> None:
        self._http = http
        self._portal = portal if portal.endswith("/") else portal + "/"
        self._host = (urlsplit(self._portal).hostname or "").lower()
        self._auth = auth
        self._min_interval = min_interval
        self._sleep = sleep
        self._clock = clock
        self._recorder = recorder
        self._lock = asyncio.Lock()
        self._next_slot = 0.0
        if isinstance(auth, WebhookAuth):
            webhook_host = (urlsplit(auth.url).hostname or "").lower()
            if webhook_host != self._host:
                raise AdapterConfigError("webhook_host_mismatch")

    @property
    def portal(self) -> str:
        return self._portal

    @property
    def refreshed_credentials(self) -> Mapping[str, str] | None:
        if isinstance(self._auth, OAuthAuth) and self._auth.refreshed:
            return self._auth.tokens.as_credentials()
        return None

    # --- вызовы -----------------------------------------------------------------

    async def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        v3: bool = False,
    ) -> dict[str, Any]:
        """Вызвать метод; вернуть весь ответ (result, total, next, time).

        v3 — REST 3.0 (адрес /rest/api/, единый формат ответа, ошибка
        объектом {code, message}); коды REST 3.0 приводятся к тем же
        нашим кодам, что у старой версии.

        Ошибка источника — AdapterError с кодом Битрикс24 в нижнем
        регистре; отвергнутая авторизация — AdapterAuthError; не тот
        scope или метод — AdapterConfigError.
        """
        refreshed = False
        backoff = iter(RATE_LIMIT_BACKOFF)
        while True:
            response = await self._post(method, params, v3=v3)
            if response.is_redirect:
                raise AdapterError("portal_moved")
            data = _json(response)
            error = data.get("error") if data is not None else None
            if data is not None and not error and "result" in data:
                self._record(method, params, data)
                return data
            code = _error_code(error)
            self._record(method, params, data if data is not None else response.text)
            if code == "expired_token" and isinstance(self._auth, OAuthAuth):
                if refreshed:
                    raise AdapterAuthError("expired_token")
                await self._refresh()
                refreshed = True
                continue
            if code == "query_limit_exceeded":
                delay = next(backoff, None)
                if delay is not None:
                    await self._sleep(delay)
                    continue
            raise _error(response.status_code, code)

    async def iterate(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Постраничный обход списочного метода по полю next (50 на страницу)."""
        start = 0
        while True:
            page = await self.call(method, {**(params or {}), "start": start})
            result = page.get("result")
            items = (
                [item for item in result if isinstance(item, dict)]
                if (isinstance(result, list))
                else []
            )
            for item in items:
                yield item
            next_start = page.get("next")
            if next_start is None or not items:
                return
            try:
                start = int(next_start)
            except (TypeError, ValueError):
                return

    async def download(self, url: str, *, max_bytes: int) -> bytes:
        """Скачать файл по подписанной ссылке из ответа метода.

        Ссылка пришла из ответа портала — это ввод клиента: принимается
        только https на хосте портала, иначе воркер можно было бы
        отправить куда угодно с заголовками и токеном.
        """
        parts = urlsplit(url)
        if (
            parts.scheme.lower() != "https"
            or (parts.hostname or "").lower() != self._host
        ):
            raise AdapterError("download_url_foreign")
        await self._pace()
        try:
            response = await self._http.download(
                url,
                max_bytes=max_bytes,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "*/*",
                    "Accept-Language": _DOWNLOAD_ACCEPT_LANGUAGE,
                    "Referer": self._portal,
                },
                timeout=DOWNLOAD_TIMEOUT,
            )
        except OutboundTooLargeError as exc:
            raise AdapterError("document_too_large") from exc
        except httpx.TimeoutException as exc:
            raise AdapterError("timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise AdapterError("network_error", retryable=True) from exc
        if response.status_code != 200:
            raise AdapterError(
                f"download_http_{response.status_code}",
                retryable=response.status_code >= 500,
            )
        content_type = response.headers.get("content-type", "").lower()
        if content_type.startswith("text/html"):
            # Ссылка истекла или нет прав: портал отдаёт страницу, а не файл.
            raise AdapterError("download_failed")
        return response.content

    # --- внутреннее ---------------------------------------------------------------

    async def _post(
        self, method: str, params: Mapping[str, Any] | None, *, v3: bool = False
    ) -> httpx.Response:
        await self._pace()
        body: dict[str, Any] = dict(params or {})
        if isinstance(self._auth, OAuthAuth):
            url = (
                f"{self._portal}rest/api/{method}"
                if v3
                else f"{self._portal}rest/{method}.json"
            )
            body["auth"] = self._auth.tokens.access_token
        else:
            base = self._auth.url.rstrip("/")
            if v3:
                # Вебхук REST 3.0: /rest/api/{user}/{code}/{method}.
                prefix, _, credentials = base.partition("/rest/")
                url = f"{prefix}/rest/api/{credentials}/{method}"
            else:
                url = f"{base}/{method}.json"
        try:
            return await self._http.post(
                url,
                json=body,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            )
        except httpx.TimeoutException as exc:
            raise AdapterError("timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise AdapterError("network_error", retryable=True) from exc

    async def _refresh(self) -> None:
        assert isinstance(self._auth, OAuthAuth)  # noqa: S101 — инвариант вызова
        self._auth.tokens = await self._auth.oauth.refresh(
            self._auth.tokens.refresh_token
        )
        self._auth.refreshed = True

    async def _pace(self) -> None:
        async with self._lock:
            now = self._clock()
            wait = self._next_slot - now
            if wait > 0:
                await self._sleep(wait)
                now = self._clock()
            self._next_slot = max(now, self._next_slot) + self._min_interval

    def _record(self, method: str, params: Mapping[str, Any] | None, data: Any) -> None:
        if self._recorder is not None:
            self._recorder(method, redact(dict(params or {})), redact(data))


def _json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


_V3_PREFIX = "bitrix_rest_v3_exception_"
_V3_CODES = {
    "insufficientscopeexception": "insufficient_scope",
    "accessdeniedexception": "access_denied",
    "entitynotfoundexception": "error_not_found",
    "validation_requestvalidationexception": "error_argument",
    "unknowndtopropertyexception": "error_argument",
    "unknownfilteroperatorexception": "error_argument",
    "invalidjsonexception": "error_argument",
}


def _error_code(error: Any) -> str:
    """Код ошибки старого REST (строка) и REST 3.0 (объект) — одной строкой."""
    if isinstance(error, dict):
        error = error.get("code", "")
    code = str(error or "").lower()
    if code.startswith(_V3_PREFIX):
        short = code.removeprefix(_V3_PREFIX)
        return _V3_CODES.get(short, f"v3_{short}")
    return code


def _error(status: int, code: str) -> AdapterError:
    if code in _AUTH_CODES:
        return AdapterAuthError(code)
    if code in _CONFIG_CODES:
        return AdapterConfigError(code)
    if code == "access_denied":
        # 401 — REST недоступен на тарифе портала (проблема подключения);
        # 400/403 — нет прав на объект (проблема одного документа).
        return AdapterConfigError(code) if status == 401 else AdapterError(code)
    if code in _RETRYABLE_CODES:
        return AdapterError(code, retryable=True)
    if code in _FATAL_CODES:
        return AdapterError(code)
    if code:
        return AdapterError(code[:64], retryable=status >= 500)
    return AdapterError(f"http_{status}", retryable=status >= 500)


_SECRET_KEYS = frozenset(
    {"auth", "access_token", "refresh_token", "client_secret", "code", "token"}
)
_SECRET_QUERY = frozenset({"auth", "token", "client_secret", "code"})


def redact(value: Any) -> Any:
    """Убрать токены из параметров и ответов перед записью в фикстуру.

    Ключи с секретами заменяются, в ссылках вырезаются параметры auth и
    token (DOWNLOAD_URL несёт access_token портала).
    """
    if isinstance(value, dict):
        return {
            key: ("<redacted>" if str(key).lower() in _SECRET_KEYS else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str) and "://" in value and "=" in value:
        parts = urlsplit(value)
        if parts.query:
            query = [
                (k, "<redacted>" if k.lower() in _SECRET_QUERY else v)
                for k, v in parse_qsl(parts.query, keep_blank_values=True)
            ]
            return urlunsplit(parts._replace(query=urlencode(query)))
    return value
