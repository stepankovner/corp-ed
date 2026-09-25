"""Пароли и токены.

Пароли — argon2id через argon2-cffi. passlib убран: он не обновлялся
с 2020 года и на Python 3.13 теряет модуль crypt; хеши совместимы
(обе библиотеки пишут стандартную PHC-строку $argon2id$…).

Access-токен — JWT HS256 на 15 минут. Refresh-токен — не JWT, а
случайная строка: в базе лежит только её sha256, отзыв и ротация
делаются записью в таблице (services/auth_service.py).
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from corp_ed.core.config import get_settings

ALGORITHM = "HS256"
ISSUER = "corp-ed"
AUDIENCE = "corp-ed-api"
ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_BYTES = 32

_REQUIRED_CLAIMS = ["exp", "iat", "nbf", "iss", "aud", "sub", "jti"]

# Параметры argon2-cffi по умолчанию (RFC 9106, «низкая память»):
# t=3, m=64 МиБ, p=4. Совпадают с тем, что писал passlib, поэтому
# существующие хеши не требуют пересчёта.
_hasher = PasswordHasher()

# Хеш случайной строки. Проверяется, когда пользователя нет: время
# ответа на «нет такой почты» и «неверный пароль» одинаковое, и по нему
# нельзя перебрать, какие адреса зарегистрированы (ASVS 6.3.8).
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(32))


def hash_password(password: str) -> str:
    """Хеширует пароль (argon2id с автоматической солью)."""
    return _hasher.hash(password)


def verify_password(plain_password: str, hashed_password: str | None) -> bool:
    """Проверяет пароль. Для hashed_password=None сверяет с фиктивным хешем.

    Возвращает False и на несовпадение, и на битый хеш в базе: второе —
    не повод отдать клиенту 500 и тем самым отличить его учётку от чужой.
    """
    if hashed_password is None:
        _verify_quietly(_DUMMY_HASH, plain_password)
        return False
    return _verify_quietly(hashed_password, plain_password)


def _verify_quietly(hashed_password: str, plain_password: str) -> bool:
    try:
        return _hasher.verify(hashed_password, plain_password)
    except (VerificationError, InvalidHashError):
        return False


def password_needs_rehash(hashed_password: str) -> bool:
    """Хеш посчитан со старыми параметрами — пересчитать при следующем входе."""
    try:
        return _hasher.check_needs_rehash(hashed_password)
    except InvalidHashError:
        return True


def create_access_token(
    user_id: UUID, tenant_id: UUID, role: str, token_version: int
) -> str:
    """Подписанный access-токен.

    role кладётся для удобства клиента и НЕ используется для проверки
    прав: роль читается из базы на каждый запрос (get_current_user).
    ver — версия токенов пользователя: смена пароля, блокировка или
    смена роли увеличивают её в базе, и все выданные ранее токены
    перестают приниматься, не дожидаясь exp.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "tenant_id": str(tenant_id),
        "role": role,
        "ver": token_version,
        "typ": ACCESS_TOKEN_TYPE,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=settings.access_token_ttl_minutes),
        "jti": uuid4().hex,
    }
    return jwt.encode(
        payload, settings.secret_key.get_secret_value(), algorithm=ALGORITHM
    )


def decode_access_token(token: str) -> dict[str, Any]:
    """Проверить подпись, срок, издателя, аудиторию и тип токена.

    algorithms задан списком из одного значения: иначе токен с
    "alg": "none" или подписью другим алгоритмом мог бы пройти.
    Бросает jwt.PyJWTError на любое нарушение.
    """
    payload: dict[str, Any] = jwt.decode(
        token,
        get_settings().secret_key.get_secret_value(),
        algorithms=[ALGORITHM],
        audience=AUDIENCE,
        issuer=ISSUER,
        options={"require": _REQUIRED_CLAIMS},
    )
    if payload.get("typ") != ACCESS_TOKEN_TYPE:
        raise jwt.InvalidTokenError("wrong token type")
    return payload


def new_refresh_token() -> str:
    """Случайный refresh-токен: 256 бит энтропии, URL-safe."""
    return secrets.token_urlsafe(REFRESH_TOKEN_BYTES)


def hash_refresh_token(token: str) -> str:
    """sha256 токена — то, что хранится в базе.

    Соль не нужна: у токена 256 бит энтропии, словаря для перебора нет.
    Утечка таблицы не даёт действующих токенов.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def generate_temporary_password() -> str:
    """Пароль, который команда или админ выдаёт новому пользователю.

    ~120 бит энтропии. Пользователь обязан сменить его при первом входе
    (users.must_change_password).
    """
    return secrets.token_urlsafe(15)
