"""OAuth 2.0 Битрикс24: обмен кода на токены и продление.

Полный протокол (документация «Полный протокол авторизации OAuth 2.0»):
браузер сотрудника → {портал}/oauth/authorize/?client_id&state →
портал возвращает его на адрес из карточки приложения с code (живёт
30 с) → GET {сервер авторизации}/oauth/token/?grant_type=
authorization_code&client_id&client_secret&code → пара токенов.
Продление — тот же адрес с grant_type=refresh_token; в ответ приходит
НОВЫЙ refresh_token, старый перестаёт действовать.

client_secret уходит только на сервер авторизации, никогда на портал:
портал может быть коробкой у клиента.
"""

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterConfigError,
    AdapterError,
    ExchangedCredentials,
)
from corp_ed.core.outbound import OutboundClient

TOKEN_TIMEOUT = 20.0
# Ошибки сервера авторизации → чья это проблема.
_CONFIG_ERRORS = {
    "invalid_client",
    "invalid_request",
    "insufficient_scope",
    "invalid_scope",
    "payment_required",
}


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str
    expires_at: int
    """Unix-время истечения access_token (expires из ответа или now + expires_in)."""
    member_id: str | None = None
    user_id: str | None = None

    def as_credentials(self) -> dict[str, str]:
        """Что кладётся в грант сотрудника (SecretBox)."""
        credentials = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": str(self.expires_at),
        }
        if self.member_id:
            credentials["member_id"] = self.member_id
        return credentials


class Bitrix24OAuth:
    def __init__(
        self,
        http: OutboundClient,
        *,
        portal: str,
        client_id: str,
        client_secret: str,
        server: str,
    ) -> None:
        self._http = http
        self._portal = portal if portal.endswith("/") else portal + "/"
        self._client_id = client_id
        self._client_secret = client_secret
        self._server = server if server.endswith("/") else server + "/"

    def authorize_url(self, state: str) -> str:
        # redirect_uri не передаётся: портал берёт его из карточки
        # приложения (документация), и подменить адрес возврата нельзя.
        query = urlencode(
            {"client_id": self._client_id, "response_type": "code", "state": state}
        )
        return f"{self._portal}oauth/authorize/?{query}"

    async def exchange(self, code: str) -> ExchangedCredentials:
        tokens = await self._token({"grant_type": "authorization_code", "code": code})
        return ExchangedCredentials(tokens.as_credentials(), tokens.user_id)

    async def refresh(self, refresh_token: str) -> TokenSet:
        return await self._token(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    async def _token(self, grant: dict[str, str]) -> TokenSet:
        params = {
            **grant,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        try:
            response = await self._http.get(
                f"{self._server}oauth/token/",
                params=params,
                headers={"Accept": "application/json"},
                timeout=TOKEN_TIMEOUT,
            )
        except httpx.TimeoutException as exc:
            raise AdapterError("oauth_timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise AdapterError("oauth_network_error", retryable=True) from exc
        data = _json(response)
        if data is None:
            raise AdapterError(
                f"oauth_http_{response.status_code}",
                retryable=response.status_code >= 500,
            )
        error = data.get("error")
        if error:
            code = str(error).lower()
            if code == "invalid_grant":
                # Код истёк или чужой, refresh_token просрочен: сотруднику
                # авторизоваться заново.
                raise AdapterAuthError("invalid_grant")
            if code in _CONFIG_ERRORS:
                raise AdapterConfigError(code)
            raise AdapterError(f"oauth_{code}"[:64])
        return _tokens(data)


def _json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _tokens(data: dict[str, Any], *, now: float | None = None) -> TokenSet:
    access = data.get("access_token")
    refresh = data.get("refresh_token")
    if not isinstance(access, str) or not isinstance(refresh, str):
        raise AdapterError("oauth_bad_response")
    expires = data.get("expires")
    if isinstance(expires, int | float) and expires > 0:
        expires_at = int(expires)
    else:
        expires_in = data.get("expires_in")
        ttl = int(expires_in) if isinstance(expires_in, int | float) else 3600
        expires_at = int(now if now is not None else time.time()) + ttl
    member_id = data.get("member_id")
    user_id = data.get("user_id")
    return TokenSet(
        access_token=access,
        refresh_token=refresh,
        expires_at=expires_at,
        member_id=str(member_id) if member_id else None,
        user_id=str(user_id) if user_id else None,
    )
