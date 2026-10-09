"""OAuth2 Nextcloud (встроенное приложение oauth2): обмен кода, продление.

Документация администратора (docs.nextcloud.com, admin_manual →
configuration_server/oauth2, 09.10.2026): клиента заводит админ в
«Администрирование → Безопасность → Клиенты OAuth 2.0» (имя и адрес
возврата — наша ручка обратного вызова), эндпоинты
/apps/oauth2/authorize и /apps/oauth2/api/v1/token (с index.php, если
красивые адреса не настроены — поэтому всегда через index.php), токен —
Bearer, только конфиденциальные клиенты, scope нет: «каждый токен —
полный доступ к аккаунту».

Чего на странице нет — сверено по исходникам сервера
(apps/oauth2/lib/Controller/OauthApiController.php и
LoginRedirectorController.php, ветка master, 09.10.2026):
- authorize принимает client_id, state, response_type=code; redirect_uri
  используется только для старых клиентов ownCloud — возврат идёт на
  адрес из регистрации клиента;
- token: grant_type authorization_code | refresh_token, client_id и
  client_secret в теле (или Basic); код живёт 10 минут и одноразовый;
- ответ: access_token, token_type=Bearer, expires_in=3600,
  refresh_token, user_id (uid — им называется папка
  /remote.php/dav/files/<uid>/);
- при каждом продлении выдаётся НОВЫЙ refresh_token, старый перестаёт
  действовать (rotateToken) — новую пару нужно сохранить;
- ошибки — {"error": ...} со статусом 400: invalid_client (секрет или
  client_id не те — вопрос к админу), invalid_request (код или refresh
  не найден, просрочен, уже обменян — вопрос к гранту сотрудника),
  invalid_grant (неизвестный grant_type). Защита от перебора может
  ответить 429.

Отзыва токена (RFC 7009) у приложения oauth2 нет: при отключении
сотрудника токен остаётся действовать, пока его не удалят в Nextcloud
(«Настройки → Безопасность → Устройства и сеансы»). revoke() честно
возвращает False.
"""

import time
from collections.abc import Callable, Mapping
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
AUTHORIZE_PATH = "index.php/apps/oauth2/authorize"
TOKEN_PATH = "index.php/apps/oauth2/api/v1/token"  # noqa: S105 — адрес
# Токен живёт час, синхронизация — раз в час: продлеваем, если до конца
# меньше пяти минут, а не ловим 401 посреди обхода.
REFRESH_AHEAD = 5 * 60
_GRANT_ERRORS = frozenset({"invalid_request", "invalid_grant"})


class NextcloudOAuth:
    def __init__(
        self,
        http: OutboundClient,
        *,
        server: str,
        client_id: str,
        client_secret: str,
    ) -> None:
        self._http = http
        self._server = server if server.endswith("/") else server + "/"
        self._client_id = client_id
        self._client_secret = client_secret

    def authorize_url(self, state: str) -> str:
        params = {"response_type": "code", "client_id": self._client_id, "state": state}
        return f"{self._server}{AUTHORIZE_PATH}?{urlencode(params)}"

    async def exchange(self, code: str) -> ExchangedCredentials:
        tokens, user_id = await self._token(
            {"grant_type": "authorization_code", "code": code}
        )
        return ExchangedCredentials(credentials(tokens, user_id), user_id or None)

    async def refresh(self, refresh_token: str) -> tuple[TokenSet, str]:
        return await self._token(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    async def revoke(self, credentials: Mapping[str, str]) -> bool:
        """Отзыва в oauth2 Nextcloud нет (см. описание модуля)."""
        return False

    async def _token(self, grant: dict[str, str]) -> tuple[TokenSet, str]:
        form = {
            **grant,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        try:
            response = await self._http.post(
                f"{self._server}{TOKEN_PATH}",
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
        if response.status_code == 429:
            raise AdapterError("rate_limited", retryable=True)
        data = json_object(response)
        if data is None:
            raise AdapterError(
                f"oauth_http_{response.status_code}",
                retryable=response.status_code >= 500,
            )
        error = data.get("error")
        if error:
            code = str(error).lower()
            if code == "invalid_client":
                raise AdapterConfigError("invalid_client")
            if code in _GRANT_ERRORS:
                raise AdapterAuthError("invalid_grant")
            raise AdapterError(safe_code(code, prefix="oauth_"))
        access = data.get("access_token")
        if not isinstance(access, str) or not access:
            raise AdapterError("oauth_bad_response")
        # Если новый refresh не пришёл, остаётся прежний.
        refresh = str(data.get("refresh_token") or grant.get("refresh_token", ""))
        expires_in = data.get("expires_in")
        ttl = int(expires_in) if isinstance(expires_in, int | float) else 3600
        user_id = data.get("user_id")
        return (
            TokenSet(
                access_token=access,
                refresh_token=refresh,
                expires_at=int(time.time()) + ttl,
            ),
            str(user_id) if isinstance(user_id, str | int) else "",
        )


def credentials(tokens: TokenSet, user_id: str) -> dict[str, str]:
    """Что хранится в гранте: пара токенов и uid сотрудника в Nextcloud."""
    saved = tokens.as_credentials()
    if user_id:
        saved["user_id"] = user_id
    return saved


@dataclass
class BearerAuth:
    """Токены сотрудника: продление заранее и один раз после 401; новая
    пара уходит в грант через refreshed_credentials адаптера."""

    tokens: TokenSet
    oauth: NextcloudOAuth
    user_id: str = ""
    refreshed: bool = False
    clock: Callable[[], float] = time.time
    _checked: bool = field(default=False, repr=False)

    async def header(self) -> str:
        if not self._checked:
            self._checked = True
            expires_at = self.tokens.expires_at
            if expires_at and expires_at - self.clock() < REFRESH_AHEAD:
                await self.refresh()
        return f"Bearer {self.tokens.access_token}"

    async def renew(self) -> bool:
        await self.refresh()
        return True

    async def refresh(self) -> None:
        if not self.tokens.refresh_token:
            raise AdapterAuthError("refresh_token_missing")
        self.tokens, user_id = await self.oauth.refresh(self.tokens.refresh_token)
        self.user_id = user_id or self.user_id
        self.refreshed = True

    @property
    def refreshed_credentials(self) -> Mapping[str, str] | None:
        return credentials(self.tokens, self.user_id) if self.refreshed else None
