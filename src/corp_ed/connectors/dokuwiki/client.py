"""JSON-RPC-клиент DokuWiki поверх OutboundClient.

По dokuwiki.org/devel:jsonrpc, devel:remote_api, devel:authentication и
коду сервера (inc/Remote/JsonRpcServer.php, Api.php; 2026-07-14c
«Mort»): каждый вызов — `POST {вики}/lib/exe/jsonrpc.php` с телом
`{"jsonrpc": "2.0", "id", "method", "params": {…}}` и заголовком
`Content-Type: application/json`; ответ — `{"result": …}`. Пакетных
вызовов и постраничности нет: список приходит одним ответом.

Авторизация — токен из профиля (`Authorization: Bearer`, с Kaos
2024-02-06; он же в `X-DokuWiki-Token` — некоторые связки веб-сервера и
PHP не передают Authorization) или логин и пароль (Basic: сервер
молча входит ими на каждый запрос; пароль с «:» не пройдёт — сервер
режет заголовок по каждому двоеточию, `explode(':')` в auth_setup).
Неверные данные — вход не состоялся, вызов идёт анонимом и получает 401.

Ошибки — `{"error": {code, message}}` с HTTP-статусом: 401 — нет входа
(-32603), 403 — вход есть, но вызов запрещён (`remoteuser`, не
суперпользователь, -32604), 404 — API выключен (`remote`, -32605) или
файла jsonrpc.php нет (DokuWiki до Kaos), 400 — ошибка метода (121 —
нет страницы, 221 — нет файла, -32601/-32603 — нет метода). В код идёт
только число, `message` (текст сервера) не логируется. Своих лимитов
частоты у DokuWiki нет; 429 и 5xx ставит прокси перед ней — ждём по
Retry-After или по шкале, затем ошибка с повтором задачи.
"""

import asyncio
import base64
import itertools
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from corp_ed.connectors.base import AdapterAuthError, AdapterConfigError, AdapterError
from corp_ed.connectors.common import (
    Recorder,
    json_object,
    redact,
    retry_after,
    safe_code,
    to_int,
)
from corp_ed.core.outbound import OutboundClient, OutboundTooLargeError

logger = structlog.get_logger()

USER_AGENT = "corp-ed-connector/1.0"
REQUEST_TIMEOUT = 60.0
RATE_LIMIT_BACKOFF = (1.0, 2.0, 4.0, 8.0)
ENDPOINT = "lib/exe/jsonrpc.php"
_RETRY_STATUSES = frozenset({429, 502, 503, 504})
_TAILS = ("/lib/exe/jsonrpc.php", "/doku.php")
# Коды RemoteException ядра (API Explorer): 121 — нет страницы или её
# ревизии, 221 — нет медиафайла.
_NOT_FOUND = frozenset({121, 221})
# Нет такого метода: -32601 по спецификации, -32603 с 400 — так отвечает
# Api::call (inc/Remote/Api.php) на незнакомое имя.
_NO_METHOD = frozenset({-32601, -32603})
_API_DISABLED = -32605

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class TokenAuth:
    token: str

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "X-DokuWiki-Token": self.token,
        }


@dataclass(frozen=True)
class BasicAuth:
    login: str
    password: str

    def headers(self) -> dict[str, str]:
        raw = f"{self.login}:{self.password}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}


def wiki_root(address: str) -> str:
    """Адрес из настройки → корень вики со слешем; хвост doku.php или
    lib/exe/jsonrpc.php (вставили адрес страницы или API) срезается."""
    root = address.strip().split("?", 1)[0].split("#", 1)[0].rstrip("/")
    for tail in _TAILS:
        if root.lower().endswith(tail):
            root = root[: -len(tail)]
    return root.rstrip("/") + "/"


class DokuWikiClient:
    def __init__(
        self,
        http: OutboundClient,
        *,
        base_url: str,
        auth: TokenAuth | BasicAuth,
        sleep: Sleep = asyncio.sleep,
        recorder: Recorder | None = None,
    ) -> None:
        self._http = http
        self._root = wiki_root(base_url)
        self._auth = auth
        self._sleep = sleep
        self._recorder = recorder
        self._ids = itertools.count(1)

    @property
    def root(self) -> str:
        return self._root

    async def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        max_bytes: int | None = None,
    ) -> Any:
        """Вызов метода; ответ — значение result. max_bytes — предел тела
        ответа (core.getMedia несёт файл в base64)."""
        payload = dict(params or {})
        body = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": payload,
        }
        backoff = iter(RATE_LIMIT_BACKOFF)
        while True:
            try:
                response = await self._http.post(
                    f"{self._root}{ENDPOINT}",
                    json=body,
                    headers={
                        **self._auth.headers(),
                        "Accept": "application/json",
                        "Content-Type": "application/json",
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
                await self._sleep(
                    retry_after(response.headers.get("retry-after")) or delay
                )
                continue
            data = json_object(response)
            if self._recorder is not None:
                self._recorder(method, redact(payload, request=True), redact(data))
            return self._result(method, status, data)

    @staticmethod
    def _result(method: str, status: int, data: dict[str, Any] | None) -> Any:
        error = (data or {}).get("error")
        code = to_int(error.get("code")) if isinstance(error, dict) else None
        if status == 200 and data is not None and not code:
            # Версия 2.0 отвечает без error, 1.x — error: null, упрощённая —
            # {code: 0, message: success}.
            return data.get("result")
        if status == 401:
            raise AdapterAuthError("unauthorized")
        if status == 403:
            # Учётка вошла, но API ей закрыт (remoteuser) или вызов требует
            # суперпользователя: чинит администратор вики.
            raise AdapterConfigError("forbidden")
        if status == 404:
            if code == _API_DISABLED:
                raise AdapterConfigError("remote_disabled")
            # Файла jsonrpc.php нет: DokuWiki до Kaos или не тот адрес.
            raise AdapterConfigError("jsonrpc_not_found")
        if data is None:
            # HTML вместо JSON — страница входа прокси или не тот адрес.
            if status >= 500:
                raise AdapterError(f"http_{status}", retryable=True)
            raise AdapterError("not_json")
        if code in _NOT_FOUND:
            raise AdapterError("not_found")
        if status == 400 and code in _NO_METHOD:
            logger.info("dokuwiki_method_missing", method=method)
            raise AdapterError("method_missing")
        if code is not None:
            raise AdapterError(
                safe_code(abs(code), prefix="dokuwiki_"), retryable=status >= 500
            )
        raise AdapterError(f"http_{status}", retryable=status >= 500)
