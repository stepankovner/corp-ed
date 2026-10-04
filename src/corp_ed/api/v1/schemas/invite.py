import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from corp_ed.api.v1.schemas.auth import MAX_TOKEN_LENGTH, TokenResponse
from corp_ed.api.v1.schemas.base import RequestModel
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
    requires_approval: bool = False
    """Вступивший ждёт одобрения администратора."""

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
    requires_approval: bool
    status: InviteStatusValue


class InviteCreatedResponse(BaseModel):
    invite: InviteResponse
    token: str
    """Показывается один раз. Ссылка — /join#<token>: токен после «#» не
    уходит на сервер и в журналы доступа."""
    code: str
    """Та же ссылка в короткой форме для диктовки (K7QM-4XPA)."""


class InviteSecretRequest(RequestModel):
    secret: str = Field(min_length=8, max_length=MAX_TOKEN_LENGTH)
    """Токен из ссылки или код приглашения."""


class InvitePreviewResponse(BaseModel):
    company_name: str
    expires_at: datetime
    email_domain: str | None
    requires_approval: bool


class JoinResponse(BaseModel):
    outcome: Literal["joined", "pending", "already_member"]
    company_name: str
    session: TokenResponse | None
    """Новая сессия в этой компании; null — ждёт одобрения админа."""
