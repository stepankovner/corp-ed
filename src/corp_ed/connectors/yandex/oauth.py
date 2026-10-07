"""OAuth Яндекс ID: обмен кода на токены, продление, общий держатель токенов.

Сверено с документацией Яндекс ID (yandex.ru/dev/id/doc/ru/, 28.09):
- авторизация: {oauth}/authorize?response_type=code&client_id&state
  [&redirect_uri][&force_confirm=yes]. Без redirect_uri Яндекс берёт
  первый адрес из настроек приложения — поэтому передаём наш, если он
  задан. force_confirm показывает выбор аккаунта: сотрудник мог быть
  вошёл в личный Яндекс, а нужен рабочий. scope не передаём — права
  берутся из регистрации приложения;
- обмен и продление: POST {oauth}/token (form), client_id и
  client_secret в теле. При продлении приходит новый refresh_token;
- «время жизни refresh-токена совпадает с временем жизни OAuth-токена»:
  после истечения продлевать уже нечем (invalid_grant). Поэтому токены
  продлеваются заранее, а не только после 401 (YandexAuth.ensure_fresh);
- ошибки — JSON {error, error_description} со статусом 400.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterConfigError,
    AdapterError,
    ExchangedCredentials,
)
from corp_ed.connectors.common import TokenSet, json_object, safe_code
from corp_ed.core.outbound import OutboundClient, OutboundTooLargeError

TOKEN_TIMEOUT = 20.0
# Продлить, если до истечения меньше месяца: синхронизация идёт раз в
# час, но сотрудник может не открывать продукт неделями.
REFRESH_AHEAD = 30 * 24 * 3600
# Код авторизации отозван, просрочен или уже использован; refresh-токен
# отозван или просрочен — вопрос к гранту сотрудника.
_GRANT_ERRORS = frozenset({"invalid_grant", "bad_verification_code"})
# Приложение отклонено, заблокировано, сменило права или неверно
# настроено — вопрос к админу, гранты сотрудников не виноваты.
_CONFIG_ERRORS = frozenset(
    {
        "invalid_client",
        "invalid_request",
        "invalid_scope",
        "unauthorized_client",
        "unsupported_grant_type",
    }
)


class YandexOAuth:
    def __init__(
        self,
        http: OutboundClient,
        *,
        client_id: str,
        client_secret: str,
        server: str,
        redirect_uri: str | None = None,
    ) -> None:
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._server = server if server.endswith("/") else server + "/"
        self._redirect_uri = redirect_uri

    def authorize_url(self, state: str) -> str:
        params = {
            "response_type": "code",
            "client_id": self._client_id,
            "state": state,
            "force_confirm": "yes",
        }
        if self._redirect_uri:
            params["redirect_uri"] = self._redirect_uri
        return f"{self._server}authorize?{urlencode(params)}"

    async def exchange(self, code: str) -> ExchangedCredentials:
        tokens = await self._token({"grant_type": "authorization_code", "code": code})
        return ExchangedCredentials(tokens.as_credentials(), None)

    async def refresh(self, refresh_token: str) -> TokenSet:
        return await self._token(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    async def _token(self, grant: dict[str, str]) -> TokenSet:
        form = {
            **grant,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        try:
            response = await self._http.post(
                f"{self._server}token",
                data=form,
                headers={"Accept": "application/json"},
                timeout=TOKEN_TIMEOUT,
                allow_redirects=False,
            )
        except OutboundTooLargeError as exc:
            raise AdapterError("oauth_response_too_large") from exc
        except httpx.TimeoutException as exc:
            raise AdapterError("oauth_timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise AdapterError("oauth_network_error", retryable=True) from exc
        data = json_object(response)
        if data is None:
            raise AdapterError(
                f"oauth_http_{response.status_code}",
                retryable=response.status_code >= 500,
            )
        error = data.get("error")
        if error:
            code = str(error).lower()
            if code in _GRANT_ERRORS:
                raise AdapterAuthError("invalid_grant")
            if code in _CONFIG_ERRORS:
                raise AdapterConfigError(code)
            raise AdapterError(safe_code(code, prefix="oauth_"))
        access = data.get("access_token")
        if not isinstance(access, str) or not access:
            raise AdapterError("oauth_bad_response")
        refresh = data.get("refresh_token")
        expires_in = data.get("expires_in")
        ttl = int(expires_in) if isinstance(expires_in, int | float) else 3600
        return TokenSet(
            access_token=access,
            # Если новый refresh не пришёл, остаётся прежний.
            refresh_token=str(refresh) if refresh else grant.get("refresh_token", ""),
            expires_at=int(time.time()) + ttl,
        )


@dataclass
class YandexAuth:
    """Токены сотрудника — общие для Диска и Вики одного подключения.

    Продление — один раз заранее (ensure_fresh) и один раз после 401;
    новая пара уходит в грант через refreshed_credentials адаптера.
    """

    tokens: TokenSet
    oauth: YandexOAuth
    refreshed: bool = False
    clock: Callable[[], float] = time.time
    _checked: bool = field(default=False, repr=False)

    @property
    def header(self) -> str:
        return f"OAuth {self.tokens.access_token}"

    async def ensure_fresh(self) -> None:
        if self._checked:
            return
        self._checked = True
        expires_at = self.tokens.expires_at
        if expires_at and expires_at - self.clock() < REFRESH_AHEAD:
            await self.refresh()

    async def refresh(self) -> None:
        refresh_token = self.tokens.refresh_token
        if not refresh_token:
            # Без refresh продлевать нечем: это грант сотрудника, а не
            # ошибка приложения — авторизоваться заново.
            raise AdapterAuthError("refresh_token_missing")
        self.tokens = await self.oauth.refresh(refresh_token)
        self.refreshed = True
