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
ACCESS_TOKEN_TYPE = "access"  # noqa: S105 — значение claim typ, не секрет
OAUTH_STATE_TYPE = "connector_oauth"  # noqa: S105 — значение claim typ, не секрет
REFRESH_TOKEN_BYTES = 32

_REQUIRED_CLAIMS = ["exp", "iat", "nbf", "iss", "aud", "sub", "jti"]
_OAUTH_STATE_CLAIMS = [*_REQUIRED_CLAIMS, "tenant_id", "connector_id"]

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
    account_id: UUID,
    account_version: int,
    *,
    tenant_id: UUID | None = None,
    member_id: UUID | None = None,
    role: str | None = None,
    member_version: int | None = None,
) -> str:
    """Подписанный access-токен учётки (ТЗ §2).

    sub — учётка, ver — её версия сессий (смена пароля, «выйти везде»).
    Если выбрана компания — tenant_id, member_id (членство) и mver —
    версия членства (смена роли, блокировка, удаление из компании): её
    рост отзывает токены этой компании, не трогая вход в остальные.
    role кладётся для удобства клиента и НЕ используется для проверки
    прав: роль читается из базы на каждый запрос (get_current_user).
    """
    settings = get_settings()
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(account_id),
        "ver": account_version,
        "typ": ACCESS_TOKEN_TYPE,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=settings.access_token_ttl_minutes),
        "jti": uuid4().hex,
    }
    if tenant_id is not None:
        payload |= {
            "tenant_id": str(tenant_id),
            "member_id": str(member_id),
            "role": role,
            "mver": member_version,
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


def create_oauth_state(
    user_id: UUID, tenant_id: UUID, connector_id: UUID, *, ttl_minutes: int
) -> str:
    """Подписанный state для OAuth-обмена коннектора (режим per_user).

    Ручка обратного вызова приходит без нашего bearer-токена — браузер
    сотрудника редиректится с портала. Кто и к какому подключению
    авторизуется, ядро узнаёт только из state, поэтому он подписан тем же
    ключом, что access-токены, с отдельным typ (access-токен в роли
    state не пройдёт и наоборот) и коротким сроком.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "tenant_id": str(tenant_id),
        "connector_id": str(connector_id),
        "typ": OAUTH_STATE_TYPE,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=ttl_minutes),
        "jti": uuid4().hex,
    }
    return jwt.encode(
        payload, settings.secret_key.get_secret_value(), algorithm=ALGORITHM
    )


def decode_oauth_state(state: str) -> dict[str, Any]:
    """Проверить state; бросает jwt.PyJWTError на любое нарушение."""
    payload: dict[str, Any] = jwt.decode(
        state,
        get_settings().secret_key.get_secret_value(),
        algorithms=[ALGORITHM],
        audience=AUDIENCE,
        issuer=ISSUER,
        options={"require": _OAUTH_STATE_CLAIMS},
    )
    if payload.get("typ") != OAUTH_STATE_TYPE:
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


# Код приглашения: 8 знаков base32 Крокфорда (без I, L, O, U — их путают
# с 1, 0 и V). 40 бит; перебор упирается в лимит запросов по IP задолго
# до попадания в одно из живых приглашений.
_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
INVITE_CODE_LENGTH = 8


def new_invite_code() -> str:
    """Код приглашения для диктовки: K7QM-4XPA."""
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(INVITE_CODE_LENGTH))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_invite_code(code: str) -> str | None:
    """Код в каноническом виде или None, если это не код.

    Регистр, пробелы и дефисы не важны; O читается как 0, I и L — как 1:
    так код, продиктованный по телефону, всё равно сработает.
    """
    cleaned = code.strip().upper().replace("-", "").replace(" ", "")
    cleaned = cleaned.translate(str.maketrans("OIL", "011"))
    if len(cleaned) != INVITE_CODE_LENGTH:
        return None
    if any(ch not in _CODE_ALPHABET for ch in cleaned):
        return None
    return cleaned


def new_numeric_code(digits: int = 6) -> str:
    """Код из письма: 6 цифр с ведущими нулями."""
    return f"{secrets.randbelow(10**digits):0{digits}d}"


def hash_secret(value: str) -> str:
    """sha256 короткого секрета (код письма, код приглашения).

    Соль не нужна для кодов с ограниченным числом попыток: перебор
    упирается в счётчик попыток и лимит запросов, а не в хеш.
    """
    return hashlib.sha256(value.encode()).hexdigest()


def generate_temporary_password() -> str:
    """Пароль, который команда выдаёт из CLI (cli reset-password).

    ~120 бит энтропии. Человек обязан сменить его при первом входе
    (accounts.must_change_password).
    """
    return secrets.token_urlsafe(15)
