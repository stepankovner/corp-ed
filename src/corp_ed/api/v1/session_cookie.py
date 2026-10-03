"""Refresh-токен в httpOnly-cookie (RISKS №44).

Браузерный скрипт токен не видит: XSS на origin приложения больше не
уносит сессию на 30 дней, только пользуется ей, пока открыта вкладка.

Без «Запомнить это устройство» (ТЗ §3) cookie — сеансовая, без срока:
браузер стирает её при закрытии.
Access-токен остаётся в теле ответа и живёт в памяти вкладки 15 минут.

Атрибуты cookie:
- HttpOnly — недоступна из JavaScript;
- SameSite=Strict — браузер не пришлёт её с чужого сайта (CSRF);
- Path=/api/v1/auth — уходит только в ручки входа, а не с каждым запросом;
- Secure — в production (в разработке фронт на http://localhost).

Ручки, которые принимают cookie (обновление и выход), дополнительно
сверяют заголовок Origin: второй рубеж против CSRF на случай старого
браузера без SameSite. Клиенты не из браузера (CLI стенда, тесты)
Origin не присылают — их запросы проходят.
"""

from urllib.parse import urlsplit

from fastapi import Request, Response

from corp_ed.api.v1.schemas.auth import MAX_TOKEN_LENGTH, TokenResponse
from corp_ed.core.config import get_http_settings, get_settings
from corp_ed.core.exceptions import PermissionError
from corp_ed.services.auth_service import TokenPair

REFRESH_COOKIE = "kronto_refresh"
REFRESH_COOKIE_PATH = "/api/v1/auth"


def session_response(response: Response, pair: TokenPair) -> TokenResponse:
    """Ответ ручки, открывающей сессию: refresh — в cookie, access — в теле."""
    set_refresh_cookie(response, pair.refresh_token, remember=pair.remember)
    return TokenResponse(access_token=pair.access_token, expires_in=pair.expires_in)


def set_refresh_cookie(response: Response, token: str, *, remember: bool) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=(
            get_settings().refresh_token_ttl_days * 24 * 60 * 60 if remember else None
        ),
        path=REFRESH_COOKIE_PATH,
        secure=get_http_settings().is_production,
        httponly=True,
        samesite="strict",
    )


def clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        REFRESH_COOKIE,
        path=REFRESH_COOKIE_PATH,
        secure=get_http_settings().is_production,
        httponly=True,
        samesite="strict",
    )


def read_refresh_cookie(request: Request) -> str | None:
    """Значение cookie или None. Слишком длинное — как отсутствующее:
    хеш от мегабайта считать незачем, настоящий токен короче."""
    value = request.cookies.get(REFRESH_COOKIE)
    if not value or len(value) > MAX_TOKEN_LENGTH:
        return None
    return value


def ensure_same_origin(request: Request) -> None:
    """Запрос с cookie пришёл со страницы нашего сайта.

    Сравнивается хост из Origin с заголовком Host (его уже проверил
    TrustedHost), без схемы: за прокси схема в приложении зависит от
    доверия к X-Forwarded-Proto, и ошибка в его настройке не должна
    разлогинивать всех. Отдельный origin фронтенда — из CORS_ALLOWED_ORIGINS.
    """
    origin = request.headers.get("origin")
    if origin is None:
        return
    if origin in get_http_settings().cors_origins:
        return
    host = request.headers.get("host", "")
    if host and urlsplit(origin).netloc == host:
        return
    raise PermissionError("Запрос пришёл не со страницы приложения")
