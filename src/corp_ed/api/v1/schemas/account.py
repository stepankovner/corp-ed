from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from corp_ed.api.v1.schemas.auth import PersonName
from corp_ed.api.v1.schemas.base import RequestModel
from corp_ed.core.password_policy import MAX_PASSWORD_LENGTH


class NameUpdateRequest(RequestModel):
    first_name: PersonName
    last_name: PersonName


class EmailChangeRequest(RequestModel):
    new_email: EmailStr
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class PasswordConfirmRequest(RequestModel):
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class LeaveCompanyRequest(RequestModel):
    tenant_id: UUID


class CompanyRequestCreate(RequestModel):
    company_name: str = Field(min_length=2, max_length=200)
    seats: int | None = Field(default=None, ge=1, le=10_000)
    """Сколько сотрудников будут пользоваться — ориентир для тарифа."""
    comment: str | None = Field(default=None, max_length=2000)


class CompanyRequestResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    company_name: str
    seats: int | None
    comment: str | None
    status: Literal["new", "approved", "rejected", "cancelled"]
    created_at: datetime
    decided_at: datetime | None


class PasskeyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    created_at: datetime
    last_used_at: datetime | None


class SecurityResponse(BaseModel):
    totp_enabled: bool
    passkeys: list[PasskeyResponse]
    backup_codes_left: int
    strong_required: bool


class TotpSetupResponse(BaseModel):
    secret: str
    """Для ручного ввода в приложение, если QR не сканируется."""
    otpauth_uri: str
    setup_token: str


class TotpEnableRequest(RequestModel):
    setup_token: str = Field(min_length=16, max_length=128)
    code: str = Field(min_length=6, max_length=12)


class SecondFactorConfirmRequest(RequestModel):
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    code: str = Field(min_length=6, max_length=32)
    """Код приложения или резервный."""


class BackupCodesResponse(BaseModel):
    backup_codes: list[str] | None
    """Показываются один раз; null — коды уже были выданы раньше."""


class PasskeySetupResponse(BaseModel):
    options: dict[str, Any]
    setup_token: str


class PasskeyRegisterRequest(RequestModel):
    setup_token: str = Field(min_length=16, max_length=128)
    credential: dict[str, Any]
    name: str = Field(default="Ключ доступа", max_length=100)


class PasskeyCreatedResponse(BaseModel):
    passkey: PasskeyResponse
    backup_codes: list[str] | None


class SessionResponse(BaseModel):
    id: UUID
    device: str | None
    ip: str | None
    started_at: datetime
    last_active_at: datetime
    current: bool
