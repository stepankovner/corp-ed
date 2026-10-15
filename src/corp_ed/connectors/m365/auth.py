"""Токен приложения Entra ID для Microsoft Graph (client credentials).

Поток OAuth 2.0 client credentials с общим секретом
(learn.microsoft.com/entra/identity-platform/v2-oauth2-client-creds-grant-flow):
`POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token`,
форма `client_id`, `client_secret`, `grant_type=client_credentials`,
`scope=https://graph.microsoft.com/.default` — токен несёт все
application permissions, на которые администратор дал согласие.
Ответ — `{token_type, expires_in, access_token}`; refresh-токена в этом
потоке нет, по истечении просто запрашивается новый. Кеш — на адаптер
(один запуск) до `expires_in` минус запас.

Отказы — `{error, error_description, error_codes: [AADSTS…]}`
(learn.microsoft.com/entra/identity-platform/reference-error-codes).
Наружу идут только error и коды: в описании — идентификаторы клиента и
трассировки.
- invalid_client: 7000215 — неверный секрет, 7000222 — секрет истёк
  (AdapterAuthError: админ вводит новый секрет);
- unauthorized_client (700016 — приложения нет в этом клиенте),
  invalid_request с 90002 / 900023 (клиент Entra не найден или указан
  неверно), invalid_scope — AdapterConfigError: чинится в настройках
  подключения или приложения, а не секретом;
- 429 и 5xx — паузы, затем AdapterError(retryable).
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx
import structlog

from corp_ed.connectors.base import AdapterAuthError, AdapterConfigError, AdapterError
from corp_ed.connectors.common import json_object, path_segment, safe_code, to_int
from corp_ed.core.outbound import OutboundClient, OutboundTooLargeError

logger = structlog.get_logger()

LOGIN_HOST = "https://login.microsoftonline.com/"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
REQUEST_TIMEOUT = 30.0
RETRY_BACKOFF = (1.0, 2.0, 4.0)
# Токен считается истёкшим заранее: запрос, начатый на последней секунде,
# не должен уйти с мёртвым токеном.
EXPIRY_MARGIN = 120
DEFAULT_LIFETIME = 3599

_SECRET_CODES = {7000215: "client_secret_invalid", 7000222: "client_secret_expired"}
_TENANT_CODES = frozenset({90002, 900023})

Sleep = Callable[[float], Awaitable[None]]


class ClientCredentials:
    def __init__(
        self,
        http: OutboundClient,
        *,
        tenant: str,
        client_id: str,
        client_secret: str,
        login_host: str = LOGIN_HOST,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._http = http
        root = login_host if login_host.endswith("/") else login_host + "/"
        self._url = f"{root}{path_segment(tenant)}/oauth2/v2.0/token"
        self._client_id = client_id
        self._secret = client_secret
        self._sleep = sleep
        self._clock = clock
        self._token: tuple[str, float] | None = None

    async def token(self, *, renew: bool = False) -> str:
        """Действующий токен Graph; renew — выбросить кеш (Graph ответил 401)."""
        if self._token is not None and not renew and self._token[1] > self._clock():
            return self._token[0]
        token, lifetime = await self._exchange()
        self._token = (token, self._clock() + max(lifetime - EXPIRY_MARGIN, 60))
        return token

    async def _exchange(self) -> tuple[str, int]:
        form = {
            "client_id": self._client_id,
            "client_secret": self._secret,
            "grant_type": "client_credentials",
            "scope": GRAPH_SCOPE,
        }
        backoff = iter(RETRY_BACKOFF)
        while True:
            try:
                response = await self._http.post(
                    self._url,
                    data=form,
                    headers={"Accept": "application/json"},
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
            if status == 429 or status >= 500:
                delay = next(backoff, None)
                if delay is None:
                    code = "rate_limited" if status == 429 else f"http_{status}"
                    raise AdapterError(code, retryable=True)
                await self._sleep(delay)
                continue
            return self._parse(response)

    def _parse(self, response: httpx.Response) -> tuple[str, int]:
        data: Mapping[str, Any] = json_object(response) or {}
        token = data.get("access_token")
        if response.status_code == 200 and isinstance(token, str) and token:
            lifetime = to_int(data.get("expires_in"))
            return token, lifetime if lifetime and lifetime > 0 else DEFAULT_LIFETIME
        error = str(data.get("error") or f"http_{response.status_code}")
        raw_codes = data.get("error_codes")
        codes = (
            [
                code
                for code in (to_int(c) for c in (raw_codes or []))
                if code is not None
            ]
            if isinstance(raw_codes, list)
            else []
        )
        logger.info("m365_token_refused", error=safe_code(error), codes=codes[:5])
        if error == "invalid_client":
            for code in codes:
                if code in _SECRET_CODES:
                    raise AdapterAuthError(_SECRET_CODES[code])
            raise AdapterAuthError("invalid_client")
        if error == "unauthorized_client":
            raise AdapterConfigError("app_not_in_tenant")
        if error == "invalid_request" and _TENANT_CODES & set(codes):
            raise AdapterConfigError("tenant_not_found")
        if error in ("invalid_request", "invalid_scope"):
            raise AdapterConfigError(safe_code(error, prefix="token_"))
        raise AdapterAuthError()
