"""Токены сервисного аккаунта Google с делегированием на домен.

Обмен JWT на токен (OAuth 2.0 для серверных приложений, «JWT bearer»):
утверждение подписано RS256 закрытым ключом сервисного аккаунта —
iss = client_email, sub = сотрудник домена, от имени которого идёт
запрос, scope — один scope через пробел, aud — адрес выдачи токенов.
Адрес выдачи — наш (настройка), а не token_uri из ключа: ключ — ввод
извне, и подписанное утверждение не должно уйти на чужой хост.

Каждый scope — своим токеном: если админ не делегировал, например,
справочник сотрудников, отказ касается только модуля «Диски
сотрудников», а не всего подключения. Токены живут час; кеш — на
(почта, scope) в пределах адаптера, то есть одного запуска.

Отказы сервера токенов ({error, error_description}):
- unauthorized_client — сервисному аккаунту не выдано делегирование на
  этот scope (консоль администратора → Безопасность → Доступ к данным и
  управление ими → Управление API → Делегирование): AdapterConfigError
  `delegation_missing_<drive|users|groups>`;
- invalid_grant — ключ удалён или отозван, или почты нет в домене
  (сотрудник удалён, заблокирован): SubjectRejectedError — для
  администратора это отказ подключению, для сотрудника — пропуск;
- invalid_client и прочее — AdapterAuthError.
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
import structlog
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from corp_ed.connectors.base import AdapterAuthError, AdapterConfigError, AdapterError
from corp_ed.connectors.common import json_object, safe_code
from corp_ed.core.outbound import OutboundClient, OutboundTooLargeError

logger = structlog.get_logger()

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
USERS_SCOPE = "https://www.googleapis.com/auth/admin.directory.user.readonly"
GROUPS_SCOPE = "https://www.googleapis.com/auth/admin.directory.group.member.readonly"
SCOPES = (DRIVE_SCOPE, USERS_SCOPE, GROUPS_SCOPE)
_SCOPE_LABELS = {DRIVE_SCOPE: "drive", USERS_SCOPE: "users", GROUPS_SCOPE: "groups"}
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"
ASSERTION_LIFETIME = 3600
# Токен считается истёкшим заранее: запрос, начатый на последней
# секунде, не должен уйти с мёртвым токеном.
EXPIRY_MARGIN = 120
REQUEST_TIMEOUT = 30.0
RETRY_BACKOFF = (1.0, 2.0, 4.0)
KEY_INVALID = "service_account_key_invalid"

Sleep = Callable[[float], Awaitable[None]]


class SubjectRejectedError(AdapterAuthError):
    """invalid_grant: ключ отозван или почта не принята как сотрудник
    домена. Для администратора — отказ подключению, для сотрудника —
    пропуск его диска."""

    def __init__(self) -> None:
        super().__init__("invalid_grant")


@dataclass(frozen=True)
class ServiceAccountKey:
    client_email: str
    private_key: rsa.RSAPrivateKey
    client_id: str = ""


def parse_key(raw: str) -> ServiceAccountKey:
    """JSON-ключ сервисного аккаунта (как его скачивает Google Cloud).

    ValueError(KEY_INVALID) — не JSON, не сервисный аккаунт, нет почты или
    закрытый ключ не читается. Содержимое ключа в сообщение не попадает.
    """
    try:
        data: Any = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get("type") != "service_account":
        raise ValueError(KEY_INVALID)
    email = data.get("client_email")
    pem = data.get("private_key")
    if not isinstance(email, str) or "@" not in email or not isinstance(pem, str):
        raise ValueError(KEY_INVALID)
    try:
        key = load_pem_private_key(pem.encode(), password=None)
    except (ValueError, TypeError) as exc:
        raise ValueError(KEY_INVALID) from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError(KEY_INVALID)
    return ServiceAccountKey(
        client_email=email.strip(),
        private_key=key,
        client_id=str(data.get("client_id") or ""),
    )


def key_problem(raw: str) -> str | None:
    """Для credentials_check: код ошибки или None."""
    try:
        parse_key(raw)
    except ValueError:
        return KEY_INVALID
    return None


class ServiceAccountAuth:
    def __init__(
        self,
        http: OutboundClient,
        key: ServiceAccountKey,
        *,
        token_url: str,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._http = http
        self._key = key
        self._token_url = token_url
        self._sleep = sleep
        self._clock = clock
        self._tokens: dict[tuple[str, str], tuple[str, float]] = {}

    async def token(self, subject: str, scope: str, *, renew: bool = False) -> str:
        """Токен от имени subject на один scope; renew — выбросить кеш
        (API ответил 401 на прежний)."""
        cache_key = (subject.casefold(), scope)
        cached = self._tokens.get(cache_key)
        if cached is not None and not renew and cached[1] > self._clock():
            return cached[0]
        token, lifetime = await self._exchange(subject, scope)
        self._tokens[cache_key] = (token, self._clock() + lifetime - EXPIRY_MARGIN)
        return token

    def _assertion(self, subject: str, scope: str) -> str:
        now = int(self._clock())
        claims = {
            "iss": self._key.client_email,
            "sub": subject,
            "scope": scope,
            "aud": self._token_url,
            "iat": now,
            "exp": now + ASSERTION_LIFETIME,
        }
        return jwt.encode(claims, self._key.private_key, algorithm="RS256")

    async def _exchange(self, subject: str, scope: str) -> tuple[str, int]:
        form = {"grant_type": GRANT_TYPE, "assertion": self._assertion(subject, scope)}
        backoff = iter(RETRY_BACKOFF)
        while True:
            try:
                response = await self._http.post(
                    self._token_url,
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
            if response.status_code == 429 or response.status_code >= 500:
                delay = next(backoff, None)
                if delay is None:
                    raise AdapterError(
                        "rate_limited"
                        if response.status_code == 429
                        else f"http_{response.status_code}",
                        retryable=True,
                    )
                await self._sleep(delay)
                continue
            return self._parse(response, scope)

    def _parse(self, response: httpx.Response, scope: str) -> tuple[str, int]:
        data: Mapping[str, Any] = json_object(response) or {}
        token = data.get("access_token")
        if response.status_code == 200 and isinstance(token, str) and token:
            lifetime = data.get("expires_in")
            return token, int(lifetime) if isinstance(lifetime, int) else 3600
        error = str(data.get("error") or f"http_{response.status_code}")
        # Описание ошибки Google — свободный текст с почтами: в журнал только код.
        logger.info("gdrive_token_refused", error=safe_code(error), scope=scope)
        if error == "unauthorized_client":
            raise AdapterConfigError(
                f"delegation_missing_{_SCOPE_LABELS.get(scope, 'scope')}"
            )
        if error == "invalid_grant":
            raise SubjectRejectedError()
        raise AdapterAuthError()
