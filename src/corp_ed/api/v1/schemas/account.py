from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from corp_ed.api.v1.schemas.auth import MAX_NAME_LENGTH
from corp_ed.api.v1.schemas.base import RequestModel
from corp_ed.core.password_policy import MAX_PASSWORD_LENGTH


class NameUpdateRequest(RequestModel):
    first_name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)
    last_name: str = Field(min_length=1, max_length=MAX_NAME_LENGTH)


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
