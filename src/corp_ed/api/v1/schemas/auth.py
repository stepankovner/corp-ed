from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from corp_ed.api.v1.schemas.base import RequestModel
from corp_ed.core.password_policy import MAX_PASSWORD_LENGTH
from corp_ed.domain.models import UserRole

# Верхние границы у всех строк: argon2 и JWT считают от всей строки,
# мегабайтное поле — дешёвый способ занять CPU.
MAX_TOKEN_LENGTH = 128


class LoginRequest(RequestModel):
    company_code: str = Field(min_length=1, max_length=63)
    email: EmailStr
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class RefreshRequest(RequestModel):
    refresh_token: str = Field(min_length=1, max_length=MAX_TOKEN_LENGTH)


class ChangePasswordRequest(RequestModel):
    current_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    new_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 — схема токена (RFC 6750), не пароль
    expires_in: int


class MeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    full_name: str | None
    role: UserRole
    tenant_id: UUID
    company_name: str
    must_change_password: bool
    last_login_at: datetime | None
