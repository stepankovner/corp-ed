from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, StringConstraints

from corp_ed.api.v1.schemas.base import RequestModel
from corp_ed.core.password_policy import MAX_PASSWORD_LENGTH
from corp_ed.domain.models import MemberStatus, UserRole

# Верхние границы у всех строк: argon2 и JWT считают от всей строки,
# мегабайтное поле — дешёвый способ занять CPU.
MAX_TOKEN_LENGTH = 128
MAX_NAME_LENGTH = 100

# Имя и фамилия: пробелы по краям не считаются, из одних пробелов — пусто.
PersonName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_NAME_LENGTH),
]


class LoginRequest(RequestModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    remember: bool = True
    """«Запомнить это устройство»: 30 дней; иначе — до закрытия браузера."""


class RegisterRequest(RequestModel):
    first_name: PersonName
    last_name: PersonName
    email: EmailStr
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    consent: Literal[True]
    """Согласие на обработку персональных данных (152-ФЗ) — обязательно."""
    invite: str | None = Field(default=None, min_length=8, max_length=MAX_TOKEN_LENGTH)
    """Ссылка или код приглашения: при закрытой регистрации пускает только с ним."""


class EmailSentResponse(BaseModel):
    """Ответ один и тот же, есть учётка или нет (по нему не перебрать адреса)."""

    email: EmailStr


class EmailRequest(RequestModel):
    email: EmailStr


class VerifyCodeRequest(RequestModel):
    email: EmailStr
    code: str = Field(pattern=r"^\s*\d{6}\s*$")


class TokenRequest(RequestModel):
    token: str = Field(min_length=16, max_length=MAX_TOKEN_LENGTH)


class ResetPasswordRequest(TokenRequest):
    new_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class ChangePasswordRequest(RequestModel):
    current_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    new_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class SwitchCompanyRequest(RequestModel):
    tenant_id: UUID | None
    """None — выйти в «без компании» (например, перед вступлением в новую)."""


class TokenResponse(BaseModel):
    """Access-токен для заголовка Authorization. Refresh-токен в тело не
    попадает: он уходит в httpOnly-cookie (api/v1/session_cookie.py)."""

    access_token: str
    token_type: str = "bearer"  # noqa: S105 — схема токена (RFC 6750), не пароль
    expires_in: int


class MembershipItem(BaseModel):
    tenant_id: UUID
    company_name: str
    role: UserRole
    status: MemberStatus


class CurrentCompany(BaseModel):
    tenant_id: UUID
    member_id: UUID
    name: str
    role: UserRole


class MeResponse(BaseModel):
    id: UUID
    """Учётка (не членство)."""
    email: EmailStr
    first_name: str | None
    last_name: str | None
    full_name: str | None
    must_change_password: bool
    last_login_at: datetime | None
    company: CurrentCompany | None
    """Выбранная компания; null — человек без компании."""
    companies: list[MembershipItem]
    """Все компании человека (кроме тех, откуда он ушёл)."""
