"""OAuth 2.0 amoCRM / Kommo: адрес авторизации, обмен кода, продление.

По документации amoCRM (www.amocrm.ru/developers/content/oauth/
step-by-step и oauth/oauth, 15.10.2026):
- авторизация: https://www.amocrm.ru/oauth?client_id&state&mode (для
  Kommo — www.kommo.com/oauth). redirect_uri в адресе нет — он берётся
  из настроек интеграции. mode=post_message — возврат на Redirect URI в
  том же окне, где открыта авторизация (popup закрывает окно и ведёт
  «основное», которого при полном переходе нет);
- возврат: code (живёт 20 минут), referer (адрес аккаунта), state,
  platform; отказ — error=access_denied. referer ядро не передаёт, и он
  не нужен: адрес аккаунта задаёт админ в config, обмен и все запросы
  идут только туда (а check сверяет поддомен аккаунта токена);
- обмен и продление: POST {аккаунт}/oauth2/access_token, JSON:
  client_id, client_secret, grant_type, code | refresh_token,
  redirect_uri — «должен четко совпадать» с настройками интеграции,
  поэтому это наша ручка обратного вызова (oauth_callback_url);
- ответ: token_type, expires_in (в примере сутки), access_token (JWT с
  id пользователя и аккаунта), refresh_token. refresh одноразовый: после
  продления старый недействителен, новую пару нужно сохранить; без
  продления 3 месяца — отзывается;
- «Токен имеет права пользователя, который предоставил доступ».

Ответы об ошибках сервера авторизации документация описывает только
кодом: 400 — неверные данные (код истёк или уже обменян, refresh
недействителен), 401 — не принята сама интеграция. Тексты не
разбираются; поле error в стиле OAuth 2.0, если придёт, учитывается.

Отзыва токена в документации нет: доступ снимает администратор
аккаунта, деактивируя интеграцию (её токены отзываются). revoke()
честно возвращает False.
"""

import re
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode, urlsplit

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
TOKEN_PATH = "oauth2/access_token"  # noqa: S105 — адрес, не секрет
# Платформы: российская (amocrm.ru) и глобальная (kommo.com). Адрес
# аккаунта, сервиса файлов и ссылки на скачивание — только на них.
PLATFORM_DOMAINS = ("amocrm.ru", "kommo.com")
_AUTHORIZE = {
    "amocrm.ru": "https://www.amocrm.ru/oauth",
    "kommo.com": "https://www.kommo.com/oauth",
}
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# Ошибки в стиле OAuth 2.0, если сервер их пришлёт.
_GRANT_ERRORS = frozenset({"invalid_grant", "invalid_request"})
_CONFIG_ERRORS = frozenset({"invalid_client", "unauthorized_client"})


def platform_of(host: str) -> str | None:
    """Домен платформы, если host — ровно один поддомен на ней."""
    host = host.lower()
    for domain in PLATFORM_DOMAINS:
        label = host.removesuffix("." + domain)
        if label != host and _LABEL.fullmatch(label):
            return domain
    return None


def account_root(address: str) -> str | None:
    """Адрес аккаунта из настройки → https://<поддомен>.<платформа>/.

    Принимается только https на поддомене amocrm.ru или kommo.com (путь,
    который админ мог вставить вместе с адресом, отбрасывается); www —
    не аккаунт. Иначе None: токены и код не уходят на чужой хост.
    """
    try:
        parts = urlsplit(address.strip())
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if (
        parts.scheme.lower() != "https"
        or port not in (None, 443)
        or parts.username is not None
        or platform_of(host) is None
        or host.startswith("www.")
    ):
        return None
    return f"https://{host}/"


def service_host_allowed(url: str, *, platform: str) -> bool:
    """Адрес сервиса из ответа аккаунта (drive_url, ссылка на файл):
    https на поддомене той же платформы, что и аккаунт."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme.lower() == "https"
        and port in (None, 443)
        and parts.username is None
        and platform_of(parts.hostname or "") == platform
    )


class AmoOAuth:
    def __init__(
        self,
        http: OutboundClient,
        *,
        account: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        root = account_root(account)
        if root is None:
            raise AdapterConfigError("account_invalid")
        self._http = http
        self._account = root
        self._platform = platform_of(urlsplit(root).hostname or "") or ""
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._clock = clock

    def authorize_url(self, state: str) -> str:
        query = urlencode(
            {"client_id": self._client_id, "state": state, "mode": "post_message"}
        )
        return f"{_AUTHORIZE[self._platform]}?{query}"

    async def exchange(self, code: str) -> ExchangedCredentials:
        tokens = await self._token({"grant_type": "authorization_code", "code": code})
        # id пользователя amoCRM даёт check (current_user_id аккаунта).
        return ExchangedCredentials(tokens.as_credentials(), None)

    async def refresh(self, refresh_token: str) -> TokenSet:
        return await self._token(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    async def revoke(self, credentials: Mapping[str, str]) -> bool:
        """Отзыва в OAuth amoCRM нет (см. описание модуля)."""
        return False

    async def _token(self, grant: dict[str, str]) -> TokenSet:
        body = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "redirect_uri": self._redirect_uri,
            **grant,
        }
        try:
            response = await self._http.post(
                f"{self._account}{TOKEN_PATH}",
                json=body,
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
        status = response.status_code
        data = json_object(response)
        if status == 200 and data is not None:
            return _tokens(data, now=self._clock())
        error = str((data or {}).get("error") or "").lower()
        if error in _CONFIG_ERRORS or status == 401:
            raise AdapterConfigError("invalid_client")
        if error in _GRANT_ERRORS or status == 400:
            # Код истёк или уже обменян, refresh недействителен:
            # сотруднику авторизоваться заново.
            raise AdapterAuthError("invalid_grant")
        if status == 429:
            raise AdapterError("rate_limited", retryable=True)
        if status == 402:
            raise AdapterConfigError("account_unpaid")
        if error:
            raise AdapterError(safe_code(error, prefix="oauth_"))
        raise AdapterError(f"oauth_http_{status}", retryable=status >= 500)


def _tokens(data: dict[str, Any], *, now: float) -> TokenSet:
    access = data.get("access_token")
    refresh = data.get("refresh_token")
    if not isinstance(access, str) or not access or not isinstance(refresh, str):
        raise AdapterError("oauth_bad_response")
    expires_in = data.get("expires_in")
    ttl = int(expires_in) if isinstance(expires_in, int | float) else 86400
    return TokenSet(
        access_token=access, refresh_token=refresh, expires_at=int(now) + ttl
    )
