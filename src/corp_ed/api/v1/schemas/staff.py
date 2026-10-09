"""Наша панель (ТЗ §9): схемы для команды kronto."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from corp_ed.api.v1.schemas.base import RequestModel
from corp_ed.domain.leads import LeadStatus
from corp_ed.domain.tariffs import Tariff

MAX_SEATS = 10_000


class StaffOverviewResponse(BaseModel):
    companies: int
    active_companies: int
    pilots_ending: int
    """Пилот закончился или заканчивается в ближайшие 7 дней."""
    requests_new: int
    accounts: int


class StaffCompanyResponse(BaseModel):
    id: UUID
    name: str
    company_code: str
    is_active: bool
    tariff: Tariff
    seats: int
    pilot_until: date | None
    members: int
    pending: int
    """Ждут одобрения администратора компании."""
    admins: list[str]
    """Почта администраторов."""
    credits_used: int
    """С начала расчётного месяца."""
    pool: int
    purchased_credits: int
    """Купленные и начисленные кредиты, которые ещё не сгорели."""
    questions_month: int
    last_question_at: datetime | None
    documents: int
    connectors: int


class StaffCompanyUpdate(RequestModel):
    """Что прислано, то и меняется. pilot_until: null — снять срок пилота."""

    tariff: Tariff | None = None
    seats: int | None = Field(default=None, ge=1, le=MAX_SEATS)
    pilot_until: date | None = None
    is_active: bool | None = None
    confirm: bool = False
    """Подтвердить места, при которых пул меньше уже потраченного."""


class StaffRequestResponse(BaseModel):
    id: UUID
    company_name: str
    seats: int | None
    comment: str | None
    status: Literal["new", "approved", "rejected", "cancelled"]
    created_at: datetime
    decided_at: datetime | None
    tenant_id: UUID | None
    applicant_email: str | None
    applicant_name: str | None


class StaffApproveRequest(RequestModel):
    tariff: Tariff = Tariff.BASE
    seats: int | None = Field(default=None, ge=1, le=MAX_SEATS)
    """Нет — сколько просили в заявке (или 10)."""
    pilot_until: date | None = None


class SpendDayResponse(BaseModel):
    day: date
    questions: int
    tokens: int
    credits: int


class SpendModelResponse(BaseModel):
    model: str
    questions: int
    input_tokens: int
    output_tokens: int


class SpendCompanyResponse(BaseModel):
    tenant_id: UUID
    name: str
    company_code: str
    questions: int
    tokens: int
    credits: int


class SpendResponse(BaseModel):
    """Расход на модель ответа (из журнала ответов). rub — оценка по
    BILLING_LLM_RUB_PER_1K_TOKENS; null — цена не задана."""

    since: date
    until: date
    questions: int
    input_tokens: int
    output_tokens: int
    credits: int
    rub: float | None
    rub_per_1k_tokens: float | None
    days: list[SpendDayResponse]
    models: list[SpendModelResponse]
    companies: list[SpendCompanyResponse]


class StaffPersonCompanyResponse(BaseModel):
    tenant_id: UUID
    company_name: str
    role: str
    status: str
    last_login_at: datetime | None


class StaffPersonResponse(BaseModel):
    """Человек для помощи со входом: что настроено, без секретов."""

    id: UUID
    email: str
    full_name: str | None
    email_verified: bool
    created_at: datetime
    last_login_at: datetime | None
    must_change_password: bool
    totp: bool
    passkeys: int
    backup_codes: int
    sessions: int
    staff: bool
    companies: list[StaffPersonCompanyResponse]


class StaffLeadResponse(BaseModel):
    id: UUID
    company_name: str
    contact_name: str
    phone: str
    email: str | None
    seats: int
    tariff: str
    preferred_date: date
    preferred_slot: str
    comment: str | None
    status: LeadStatus
    created_at: datetime


class StaffLeadUpdate(RequestModel):
    status: LeadStatus
