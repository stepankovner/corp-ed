"""Общее для адаптеров: пара токенов OAuth, запись фикстур, мелкие
разборы значений. Адаптеры не импортируют друг друга."""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

OAUTH_CALLBACK_PATH = "/api/v1/connectors/oauth/callback"

Recorder = Callable[[str, Mapping[str, Any], Any], None]
"""(метод или путь, параметры без секретов, ответ без секретов) — для
записи контрактных фикстур из cli connector-check --record."""


@dataclass(frozen=True)
class TokenSet:
    """Пара токенов OAuth сотрудника: что хранится в гранте."""

    access_token: str
    refresh_token: str
    expires_at: int
    """Unix-время истечения access_token."""
    member_id: str | None = None
    user_id: str | None = None

    def as_credentials(self) -> dict[str, str]:
        credentials = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": str(self.expires_at),
        }
        if self.member_id:
            credentials["member_id"] = self.member_id
        return credentials


def json_object(response: httpx.Response) -> dict[str, Any] | None:
    """Тело ответа как объект JSON; всё остальное (HTML, список) — None."""
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


_SECRET_KEYS = frozenset(
    {"auth", "access_token", "refresh_token", "client_secret", "token", "password"}
)
# В параметрах запроса code — одноразовый код авторизации; в ответах
# CODE — символьный код сайта или страницы, а code — код ошибки REST 3.0.
_SECRET_REQUEST_KEYS = _SECRET_KEYS | {"code"}
_SECRET_QUERY = frozenset({"auth", "token", "client_secret", "code"})
_SECRET_HEADER = re.compile(r"^(Bearer|OAuth|Basic)\s+\S+", re.IGNORECASE)


# Код входящего вебхука Битрикс24 живёт в пути: /rest/{user}/{code}/…
# Портал подставляет его в DOWNLOAD_URL файлов диска — без маски он
# уехал бы в фикстуры (замечено на живой записи 26.09).
_WEBHOOK_PATH = re.compile(r"(/rest/\d+/)[^/?#]+(/)")


def redact(value: Any, *, request: bool = False) -> Any:
    """Убрать токены из параметров и ответов перед записью в фикстуру.

    Ключи с секретами заменяются, в ссылках вырезаются параметры auth и
    token (DOWNLOAD_URL Битрикс24 несёт access_token портала) и код
    вебхука из пути.
    """
    secret = _SECRET_REQUEST_KEYS if request else _SECRET_KEYS
    if isinstance(value, dict):
        return {
            key: (
                "<redacted>"
                if str(key).lower() in secret
                else redact(item, request=request)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, request=request) for item in value]
    if isinstance(value, str):
        if _SECRET_HEADER.match(value):
            return "<redacted>"
        if "://" in value:
            value = _WEBHOOK_PATH.sub(r"\1<redacted>\2", value)
        if "://" in value and "=" in value:
            parts = urlsplit(value)
            if parts.query:
                query = [
                    (k, "<redacted>" if k.lower() in _SECRET_QUERY else v)
                    for k, v in parse_qsl(parts.query, keep_blank_values=True)
                ]
                return urlunsplit(parts._replace(query=urlencode(query)))
    return value
