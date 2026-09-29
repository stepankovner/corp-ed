import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from corp_ed.api.v1.schemas.auth import MAX_TOKEN_LENGTH
from corp_ed.api.v1.schemas.base import RequestModel
from corp_ed.core.password_policy import MAX_PASSWORD_LENGTH
from corp_ed.services.invite_service import DEFAULT_TTL_DAYS, MAX_TTL_DAYS, MAX_USES

# Имя хоста: метки из латиницы, цифр и дефиса через точку, хотя бы одна
# точка. Кириллические домены — в punycode (xn--…).
_DOMAIN = re.compile(
    r"^(?=.{3,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$"
)


class InviteCreateRequest(RequestModel):
    ttl_days: int = Field(default=DEFAULT_TTL_DAYS, ge=1, le=MAX_TTL_DAYS)
    max_uses: int | None = Field(default=None, ge=1, le=MAX_USES)
    """Пусто — по числу мест компании."""
    email_domain: str | None = Field(default=None, max_length=253)
    """Только почты этого домена (и поддоменов), например acme.ru."""

    @field_validator("email_domain")
    @classmethod
    def _domain(cls, value: str | None) -> str | None:
        if value is None:
            return None
        domain = value.strip().lstrip("@").casefold()
        if not domain:
            return None
        if not _DOMAIN.fullmatch(domain):
            raise ValueError("Домен почты — например, acme.ru")
        return domain


InviteStatusValue = Literal["active", "expired", "revoked", "used_up"]


class InviteResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    expires_at: datetime
    max_uses: int
    uses: int
    email_domain: str | None
    status: InviteStatusValue


class InviteCreatedResponse(BaseModel):
    invite: InviteResponse
    token: str
    """Показывается один раз. Ссылка — /join/<код компании>#<token>:
    токен после «#» не уходит на сервер и в журналы доступа."""
    company_code: str


class InviteTokenRequest(RequestModel):
    company_code: str = Field(min_length=1, max_length=63)
    token: str = Field(min_length=16, max_length=MAX_TOKEN_LENGTH)


class InvitePreviewResponse(BaseModel):
    company_name: str
    expires_at: datetime
    email_domain: str | None


class InviteAcceptRequest(InviteTokenRequest):
    email: EmailStr
    full_name: str | None = Field(default=None, max_length=200)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
