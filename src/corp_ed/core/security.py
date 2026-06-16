from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt
from passlib.context import CryptContext

from corp_ed.core.config import settings

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 8

_pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


def hash_password(password: str) -> str:
    """Хеширует пароль (argon2 с автоматической солью)."""
    return _pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Проверяет соответствие пароля его хешу."""
    return _pwd_context.verify(plain_password, hashed_password)


def create_access_token(user_id: UUID, tenant_id: UUID, role: str) -> str:
    expire = datetime.now(UTC) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    payload = {
        "sub": str(user_id),
        "tenant_id": str(tenant_id),
        "role": role,
        "exp": expire,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
