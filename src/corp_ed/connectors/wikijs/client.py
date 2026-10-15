"""GraphQL-клиент Wiki.js 2.x поверх OutboundClient.

По docs.requarks.io/dev/api и коду сервера (requarks/wiki v2.5.315,
`server/graph`, `server/core/auth.js`, `server/master.js`; 15.10): один
адрес `POST {сайт}/graphql`, `Authorization: Bearer <API-ключ>`.

Как Wiki.js отвечает (сверено на стенде 2.5.316):

- 200 с `data` и, возможно, `errors` — ошибки полей: `Forbidden` (нет
  права), `extensions.exception.code` 6003 (страницы нет), 1019 (запрос
  обслужен как гость). Частичный ответ отдаётся вызывающему: одна
  пропавшая страница в пакетном запросе не роняет остальные.
- Неизвестный или испорченный ключ Wiki.js не отвергает: запрос идёт от
  имени гостя (это видит проверка в адаптере).
- 500 `{errors: [{message: "API Key is invalid or was revoked."}]}` —
  отозванный ключ; `API is disabled…` — API выключен в «Администрирование
  → API»; прочие 5xx, 429 — повтор с паузой.
- 400 — запрос не прошёл проверку схемы (другая версия Wiki.js).

Текст ошибок сервера (с трассировкой стека в `extensions`) в журнал и
коды не попадает: только наш код через safe_code.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from corp_ed.connectors.base import AdapterAuthError, AdapterConfigError, AdapterError
from corp_ed.connectors.common import Recorder, json_object, redact, retry_after
from corp_ed.core.outbound import OutboundClient, OutboundTooLargeError

logger = structlog.get_logger()

USER_AGENT = "corp-ed-connector/1.0"
REQUEST_TIMEOUT = 60.0
BACKOFF = (1.0, 2.0, 4.0, 8.0)
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_ENDPOINT = "graphql"
_REVOKED = "API Key is invalid"
_DISABLED = "API is disabled"

Sleep = Callable[[float], Awaitable[None]]


def site_root(address: str) -> str:
    """Адрес из настройки → корень сайта со слешем; хвост /graphql срезается."""
    root = address.strip().rstrip("/")
    if root.lower().endswith(f"/{_ENDPOINT}"):
        root = root[: -len(_ENDPOINT) - 1]
    return root.rstrip("/") + "/"


@dataclass(frozen=True)
class FieldError:
    """Ошибка одного поля ответа: путь (["pages", "p12"]) и числовой код
    Wiki.js (6003 — нет страницы) или None (Forbidden и прочие)."""

    path: tuple[str, ...]
    code: int | None
    forbidden: bool


@dataclass(frozen=True)
class GraphResult:
    data: dict[str, Any]
    errors: tuple[FieldError, ...] = field(default_factory=tuple)

    def error_at(self, *path: str) -> FieldError | None:
        """Ошибка поля по пути или внутри него."""
        for error in self.errors:
            if error.path[: len(path)] == path:
                return error
        return None


class WikiJsClient:
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

    async def query(
        self,
        name: str,
        query: str,
        variables: Mapping[str, Any] | None = None,
    ) -> GraphResult:
        """Запрос; name — для журнала и записи фикстур."""
        payload = {"query": query, "variables": dict(variables or {})}
        backoff = iter(BACKOFF)
        while True:
            try:
                response = await self._http.post(
                    f"{self._root}{_ENDPOINT}",
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
            body = json_object(response)
            message = _first_message(body)
            if status == 500 and message.startswith(_REVOKED):
                raise AdapterAuthError("unauthorized")
            if status == 500 and message.startswith(_DISABLED):
                raise AdapterConfigError("api_disabled")
            if status in _RETRY_STATUSES:
                delay = next(backoff, None)
                if delay is None:
                    code = "rate_limited" if status == 429 else f"http_{status}"
                    raise AdapterError(code, retryable=True)
                wait = retry_after(response.headers.get("retry-after"))
                await self._sleep(wait or delay)
                continue
            if self._recorder is not None:
                self._recorder(
                    name,
                    redact(payload["variables"], request=True),
                    redact(_without_traces(body)),
                )
            if status == 401:
                raise AdapterAuthError("unauthorized")
            if status == 403:
                raise AdapterAuthError("forbidden")
            if status == 404:
                # Не тот адрес: на месте /graphql у Wiki.js 2 — страница вики.
                raise AdapterConfigError("graphql_not_found")
            if status == 400:
                logger.info("wikijs_query_rejected", query=name)
                raise AdapterError("query_rejected")
            if status != 200:
                raise AdapterError(f"http_{status}", retryable=status >= 500)
            if body is None or not isinstance(body.get("data"), dict):
                # HTML вместо JSON — не Wiki.js или страница входа прокси.
                raise AdapterError("not_json")
            return GraphResult(body["data"], _errors(body))


def _first_message(body: Mapping[str, Any] | None) -> str:
    errors = (body or {}).get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return str(errors[0].get("message") or "")
    return ""


def _errors(body: Mapping[str, Any]) -> tuple[FieldError, ...]:
    found = []
    for error in body.get("errors") or []:
        if not isinstance(error, dict):
            continue
        path = tuple(str(part) for part in error.get("path") or [])
        exception = (error.get("extensions") or {}).get("exception") or {}
        code = exception.get("code") if isinstance(exception, dict) else None
        found.append(
            FieldError(
                path=path,
                code=code if isinstance(code, int) else None,
                forbidden=error.get("message") == "Forbidden",
            )
        )
    return tuple(found)


def _without_traces(body: Mapping[str, Any] | None) -> Any:
    """Ответ для записи: ошибки — только путь и код, без стека сервера."""
    if body is None:
        return None
    errors = [
        {"path": list(e.path), "code": e.code, "forbidden": e.forbidden}
        for e in _errors(body)
    ]
    return {"data": body.get("data"), "errors": errors} if errors else body
