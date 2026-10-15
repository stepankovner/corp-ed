"""Клиент Action API MediaWiki (`api.php`) поверх OutboundClient.

По документации mediawiki.org (API:Main_page, API:Login, API:Query,
API:Continue, API:Errors_and_warnings, API:Etiquette) и ответам MediaWiki
1.43 на стенде (`tests/live/mediawiki`, 15.10):

- вход — бот-пароль (Служебная:BotPasswords): `meta=tokens&type=login`
  без сессии → `action=login` (POST, `lgname=Логин@бот`, `lgpassword`,
  `lgtoken`) с cookie из первого ответа → `{login: {result: Success}}` и
  cookie сессии бота (`<wiki>_BPsession`). Неверный пароль — `result:
  Failed`, вход основным паролем запрещён настройкой вида (бот-пароль
  ограничен правами `grants`, основной пароль — нет);
- cookie хранит сам клиент (общий OutboundClient их не хранит и чужих не
  шлёт, core/outbound.py) и отправляет только на хост api.php;
- каждый запрос после входа несёт `assert=user`: истёкшая сессия (по
  умолчанию час) иначе молча превратилась бы в анонима — на открытой
  вики без ошибки. `assertuserfailed` → один повторный вход;
- ошибки API приходят с HTTP 200 в `{error: {code, info}}`: в код идёт
  только `code` через safe_code, `info` (текст сервера) не логируется.
  `readapidenied` — у бота нет права чтения (AdapterAuthError);
  `ratelimited` и `maxlag` (реплики БД отстали, Retry-After) — повтор
  с паузой, как 429 и 5xx;
- продолжение списков — объект `continue` целиком обратно в запрос
  (API:Continue), `formatversion=2`.

Файлы (`/images/…`) скачиваются только с хоста api.php и с cookie
сессии. img_auth.php сессию бот-пароля не принимает (она действует лишь
в api.php и rest.php) — такие файлы адаптер не берёт.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
import structlog

from corp_ed.connectors.base import AdapterAuthError, AdapterConfigError, AdapterError
from corp_ed.connectors.common import (
    Recorder,
    json_object,
    redact,
    retry_after,
    safe_code,
)
from corp_ed.core.outbound import OutboundClient, OutboundTooLargeError

logger = structlog.get_logger()

USER_AGENT = "corp-ed-connector/1.0 (MediaWiki API; organization sync)"
REQUEST_TIMEOUT = 60.0
DOWNLOAD_TIMEOUT = 120.0
BACKOFF = (1.0, 2.0, 4.0, 8.0)
MAXLAG = "5"
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_RETRY_CODES = frozenset({"ratelimited", "maxlag", "readonly"})
_SESSION_LOST = frozenset({"assertuserfailed", "assertnameduserfailed"})
_HIDDEN_PARAMS = frozenset({"lgpassword", "lgtoken"})

Sleep = Callable[[float], Awaitable[None]]


def api_endpoint(address: str) -> str:
    """Адрес из настройки → адрес api.php: полный адрес как есть, иначе
    корень вики (`https://wiki.company.ru/w/`) + api.php."""
    url = address.strip()
    path = urlsplit(url).path
    if path.endswith("/api.php"):
        return url.split("?", 1)[0]
    return url.split("?", 1)[0].rstrip("/") + "/api.php"


class MediaWikiClient:
    def __init__(
        self,
        http: OutboundClient,
        *,
        api_url: str,
        username: str,
        password: str,
        sleep: Sleep = asyncio.sleep,
        recorder: Recorder | None = None,
    ) -> None:
        self._http = http
        self._api = api_endpoint(api_url)
        self._host = (urlsplit(self._api).hostname or "").lower()
        self._username = username
        self._password = password
        self._sleep = sleep
        self._recorder = recorder
        self._cookies: dict[str, str] = {}
        self._logged_in = False

    @property
    def api_url(self) -> str:
        return self._api

    def same_host(self, url: str) -> bool:
        return (urlsplit(url).hostname or "").lower() == self._host

    def absolute(self, link: str) -> str:
        """Ссылка из ответа (в том числе `//host/…`) → абсолютная на хосте
        api.php. Чужой хост не принимается."""
        url = urljoin(self._api, link)
        if urlsplit(url).scheme != "https" or not self.same_host(url):
            raise AdapterError("link_foreign")
        return url

    # --- вход ---------------------------------------------------------------------

    async def login(self) -> None:
        self._cookies.clear()
        self._logged_in = False
        tokens = await self._send(
            {"action": "query", "meta": "tokens", "type": "login"}, logged_in=False
        )
        token = (tokens.get("query") or {}).get("tokens", {}).get("logintoken")
        if not isinstance(token, str) or not token:
            raise AdapterError("login_token_missing")
        answer = await self._send(
            {
                "action": "login",
                "lgname": self._username,
                "lgpassword": self._password,
                "lgtoken": token,
            },
            logged_in=False,
            post=True,
        )
        login = answer.get("login")
        result = str(login.get("result") or "") if isinstance(login, dict) else ""
        if result == "Success":
            self._logged_in = True
            return
        logger.info("mediawiki_login_refused", result=safe_code(result))
        if result == "Throttled":
            raise AdapterError("login_throttled", retryable=True)
        if result == "Aborted":
            # Бот-пароли выключены ($wgEnableBotPasswords) или вход через
            # action=login запрещён: заново эти данные не примут.
            raise AdapterAuthError("login_aborted")
        if result in {"NeedToken", "WrongToken"}:
            raise AdapterError("login_token_rejected", retryable=True)
        raise AdapterAuthError("unauthorized")

    # --- запросы --------------------------------------------------------------------

    async def call(
        self,
        params: Mapping[str, Any],
        *,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Запрос от имени бота; ответ — объект целиком."""
        if not self._logged_in:
            await self.login()
        relogged = False
        while True:
            try:
                return await self._send(params, logged_in=True, max_bytes=max_bytes)
            except _SessionLostError:
                if relogged:
                    raise AdapterAuthError("session_lost") from None
                relogged = True
                await self.login()

    async def query(self, params: Mapping[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """`action=query` с продолжением: по одному объекту `query` на ответ."""
        base = {"action": "query", **params}
        extra: dict[str, Any] = {}
        while True:
            answer = await self.call({**base, **extra})
            found = answer.get("query")
            if isinstance(found, dict):
                yield found
            more = answer.get("continue")
            if not isinstance(more, dict) or not more:
                return
            if more == extra:
                # Сервер вернул то же продолжение — иначе бесконечный цикл.
                raise AdapterError("continue_stuck")
            extra = {str(k): v for k, v in more.items()}

    async def download(self, link: str, *, max_bytes: int) -> bytes:
        url = self.absolute(link)
        if not self._logged_in:
            await self.login()
        try:
            downloaded = await self._http.download(
                url,
                max_bytes=max_bytes,
                headers=self._headers(),
                timeout=DOWNLOAD_TIMEOUT,
                same_host=True,
            )
        except OutboundTooLargeError as exc:
            raise AdapterError("document_too_large") from exc
        except httpx.TimeoutException as exc:
            raise AdapterError("timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise AdapterError("network_error", retryable=True) from exc
        status = downloaded.status_code
        if status in (401, 403):
            # img_auth.php: файл закрыт для бота — пропуск файла, не стоп.
            raise AdapterError("download_forbidden")
        if status == 404:
            raise AdapterError("not_found")
        if status != 200:
            raise AdapterError(f"download_http_{status}", retryable=status >= 500)
        return downloaded.content

    async def _send(
        self,
        params: Mapping[str, Any],
        *,
        logged_in: bool,
        post: bool = False,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        payload = {
            # Логический параметр MediaWiki истинен, если он есть вообще:
            # False и None не отправляются.
            **{
                k: _value(v)
                for k, v in params.items()
                if v is not None and v is not False
            },
            "format": "json",
            "formatversion": "2",
            "maxlag": MAXLAG,
        }
        if logged_in:
            payload["assert"] = "user"
        backoff = iter(BACKOFF)
        while True:
            try:
                if post:
                    response = await self._http.post(
                        self._api,
                        data=payload,
                        headers=self._headers(),
                        timeout=REQUEST_TIMEOUT,
                        allow_redirects=False,
                        max_bytes=max_bytes,
                    )
                else:
                    response = await self._http.get(
                        self._api,
                        params=payload,
                        headers=self._headers(),
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
            self._remember_cookies(response)
            status = response.status_code
            data = json_object(response)
            error = data.get("error") if data is not None else None
            code = str(error.get("code") or "") if isinstance(error, dict) else ""
            if status in _RETRY_STATUSES or _transient(code):
                delay = next(backoff, None)
                if delay is None:
                    if code:
                        raise AdapterError(safe_code(code), retryable=True)
                    name = "rate_limited" if status == 429 else f"http_{status}"
                    raise AdapterError(name, retryable=True)
                wait = retry_after(response.headers.get("retry-after"))
                await self._sleep(wait if wait is not None else delay)
                continue
            self._record(payload, data)
            if status == 401:
                raise AdapterAuthError("unauthorized")
            if status == 403:
                # Перед вики — прокси или WAF, который не пускает kronto.
                raise AdapterConfigError("forbidden")
            if status in (301, 302, 303, 307, 308):
                raise AdapterConfigError("api_url_redirected")
            if status == 404:
                raise AdapterConfigError("api_not_found")
            if status != 200:
                raise AdapterError(f"http_{status}", retryable=status >= 500)
            if data is None:
                # HTML вместо JSON — страница вики или входа, не api.php.
                raise AdapterConfigError("not_mediawiki_api")
            if not code:
                return data
            if code in _SESSION_LOST and logged_in:
                raise _SessionLostError(code)
            if code == "readapidenied":
                raise AdapterAuthError("read_denied")
            logger.info("mediawiki_api_error", code=safe_code(code))
            raise AdapterError(safe_code(code, prefix="mediawiki_"))

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if self._cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
        return headers

    def _remember_cookies(self, response: httpx.Response) -> None:
        """Set-Cookie ответа api.php → своя банка (имя → значение).
        Удаление (`deleted`, Max-Age=0, срок в прошлом) — убрать cookie."""
        for header in response.headers.get_list("set-cookie"):
            pair, _, attributes = header.partition(";")
            name, sep, value = pair.partition("=")
            name = name.strip()
            if not sep or not name:
                continue
            lowered = attributes.lower().replace(" ", "")
            if value.strip() in {"", "deleted"} or "max-age=0" in lowered:
                self._cookies.pop(name, None)
            else:
                self._cookies[name] = value.strip()

    def _record(self, payload: Mapping[str, Any], data: Any) -> None:
        if self._recorder is None:
            return
        params = {
            k: ("<redacted>" if k in _HIDDEN_PARAMS else v) for k, v in payload.items()
        }
        method = str(payload.get("action") or "")
        for key in ("list", "generator", "meta", "prop"):
            if key in payload:
                method = f"{method}.{payload[key]}"
                break
        self._recorder(method, redact(params, request=True), _without_tokens(data))


class _SessionLostError(Exception):
    """Сессия бота истекла (assert=user не прошёл): нужен повторный вход."""


def _transient(code: str) -> bool:
    """Код ошибки, после которого стоит повторить: лимит, отставание
    реплик, режим только чтения, сбой сервера (internal_api_error_*)."""
    return code in _RETRY_CODES or code.startswith("internal_api_error")


def _value(value: Any) -> str:
    if value is True:
        return "1"
    if isinstance(value, list | tuple | set | frozenset):
        return "|".join(str(item) for item in value)
    return str(value)


def _without_tokens(data: Any) -> Any:
    """Ответ без токенов входа (logintoken) — перед записью в фикстуру."""
    if isinstance(data, dict):
        return {
            key: (
                "<redacted>"
                if str(key).lower().endswith("token")
                else _without_tokens(v)
            )
            for key, v in data.items()
        }
    if isinstance(data, list):
        return [_without_tokens(item) for item in data]
    return redact(data)
