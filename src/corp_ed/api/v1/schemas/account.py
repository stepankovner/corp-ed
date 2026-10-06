from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from corp_ed.api.v1.schemas.auth import PersonName, ProfileText
from corp_ed.api.v1.schemas.base import RequestModel
from corp_ed.core.password_policy import MAX_PASSWORD_LENGTH


class ProfileUpdateRequest(RequestModel):
    """Личное в профиле (ТЗ §4). Пришедшее null — очистить, не пришедшее —
    не трогать. Имя и фамилия, если пришли, не пустые."""

    first_name: PersonName | None = None
    last_name: PersonName | None = None
    patronymic: ProfileText | None = None
    phone: str | None = Field(
        default=None,
        max_length=32,
        description="Хранится как +79991234567: «+7 (999) 123-45-67» и "
        "«8 999 123 45 67» приводятся к этому виду; с кодом страны, 10–15 цифр.",
    )
    telegram: str | None = Field(
        default=None,
        max_length=64,
        description="Имя без «@»: «@anna_s» и «https://t.me/anna_s» хранятся как "
        "anna_s; 5–32 латинских буквы, цифры и «_», с буквы — правило Telegram.",
    )

    @model_validator(mode="after")
    def names_are_required(self) -> Self:
        for field in ("first_name", "last_name"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} не может быть пустым")
        return self


class AvatarResponse(BaseModel):
    avatar_url: str


class EmailChangeRequest(RequestModel):
    new_email: EmailStr
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    code: str | None = Field(default=None, min_length=1, max_length=16)
    """Второй фактор: код приложения или резервный; без приложения — код,
    пришедший на прежний адрес после первого запроса без него."""


class EmailChangeResponse(BaseModel):
    status: Literal["code_sent", "link_sent"]
    """code_sent — код ушёл на прежний адрес; link_sent — ссылка на новый."""
    email_hint: str | None


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
