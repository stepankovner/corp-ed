import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

from corp_ed.api.v1.schemas.base import RequestModel
from corp_ed.domain.leads import LeadTariff

_PHONE = re.compile(r"^\+?[0-9 ()\-]{10,24}$")


class LeadRequest(RequestModel):
    company_name: str = Field(min_length=2, max_length=200)
    contact_name: str = Field(min_length=2, max_length=200)
    phone: str = Field(min_length=10, max_length=24)
    email: EmailStr | None = None
    seats: int = Field(ge=1, le=100_000)
    """Сколько сотрудников работают за компьютером — первый вопрос созвона
    (досье 5.4)."""
    tariff: LeadTariff = LeadTariff.BASE
    preferred_date: date
    preferred_slot: str = Field(min_length=1, max_length=16)
    comment: str | None = Field(default=None, max_length=1000)
    policy_version: str = Field(min_length=1, max_length=64)
    consent: bool
    website: str = Field(default="", max_length=200)
    """Ловушка для ботов: поле скрыто от людей, заполненное — заявка
    молча не сохраняется."""

    @field_validator("company_name", "contact_name", "comment")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str) -> str:
        value = value.strip()
        digits = re.sub(r"\D", "", value)
        if not _PHONE.fullmatch(value) or not 10 <= len(digits) <= 15:
            raise ValueError("Телефон — например, +7 999 123-45-67")
        return ("+" if value.startswith("+") else "") + digits


class LeadFormResponse(BaseModel):
    """Что нужно форме записи: открыта ли она, политика, даты и окна."""

    enabled: bool
    policy_url: str | None
    policy_version: str | None
    slots: list[str]
    first_date: date
    last_date: date
    timezone: str


class LeadReceivedResponse(BaseModel):
    status: Literal["received"] = "received"
