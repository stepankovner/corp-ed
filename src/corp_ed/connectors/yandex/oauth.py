"""OAuth Яндекс ID: обмен кода на токены и продление.

Авторизация: {oauth}/authorize?response_type=code&client_id&state —
адрес возврата берётся из настроек приложения на oauth.yandex.ru.
Обмен: POST {oauth}/token (form) grant_type=authorization_code&code&
client_id&client_secret → access_token, refresh_token, expires_in
(обычно год). Продление: grant_type=refresh_token. Ошибки — JSON
{error, error_description} со статусом 400.
"""

import time
from typing import Any
from urllib.parse import urlencode

import httpx

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterConfigError,
    AdapterError,
    ExchangedCredentials,
)
from corp_ed.connectors.bitrix24.oauth import TokenSet
from corp_ed.core.outbound import OutboundClient

TOKEN_TIMEOUT = 20.0
_CONFIG_ERRORS = frozenset({"invalid_client", "invalid_request", "invalid_scope"})


class YandexOAuth:
    def __init__(
        self,
        http: OutboundClient,
        *,
        client_id: str,
        client_secret: str,
        server: str,
    ) -> None:
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._server = server if server.endswith("/") else server + "/"

    def authorize_url(self, state: str) -> str:
        query = urlencode(
            {"response_type": "code", "client_id": self._client_id, "state": state}
        )
        return f"{self._server}authorize?{query}"

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
                raise AdapterAuthError("invalid_grant")
            if code in _CONFIG_ERRORS:
                raise AdapterConfigError(code)
            raise AdapterError(f"oauth_{code}"[:64])
        access = data.get("access_token")
        if not isinstance(access, str) or not access:
            raise AdapterError("oauth_bad_response")
        refresh = data.get("refresh_token")
        expires_in = data.get("expires_in")
        ttl = int(expires_in) if isinstance(expires_in, int | float) else 3600
        return TokenSet(
            access_token=access,
            # Яндекс может не выдать новый refresh при продлении: старый остаётся.
            refresh_token=str(refresh) if refresh else grant.get("refresh_token", ""),
            expires_at=int(time.time()) + ttl,
        )


def _json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
