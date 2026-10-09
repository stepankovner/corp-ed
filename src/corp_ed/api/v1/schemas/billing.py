from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from corp_ed.api.v1.schemas.base import RequestModel
from corp_ed.domain.tariffs import Tariff

PeriodName = Literal["month", "quarter", "year"]
MethodName = Literal["invoice", "card"]
InvoiceKind = Literal["subscription", "seats", "credits"]
InvoiceStatus = Literal["awaiting_payment", "paid", "cancelled"]
SubscriptionStatus = Literal["awaiting_payment", "active", "overdue", "cancelled"]
PaymentStatus = Literal[
    "received", "pending", "matched", "mismatch", "unmatched", "ignored", "resolved"
]


class RequisitesRequest(RequestModel):
    """Реквизиты компании для счёта: ИНН и КПП проверяются (контрольные
    цифры), у ИП КПП пустой."""

    legal_name: str = Field(min_length=2, max_length=300, pattern=r"\S")
    inn: str = Field(min_length=10, max_length=12)
    kpp: str | None = Field(default=None, max_length=9)
    address: str = Field(min_length=5, max_length=500, pattern=r"\S")
    documents_email: EmailStr | None = None
    """Куда слать акты и чеки; пусто — на почту администраторов."""


class RequisitesResponse(BaseModel):
    legal_name: str
    inn: str
    kpp: str | None
    payer_type: Literal["company", "ip"]
    address: str
    documents_email: str | None
    updated_at: datetime


class QuoteResponse(BaseModel):
    period: PeriodName
    discount_percent: float
    amount_kopecks: int


class SubscriptionResponse(BaseModel):
    tariff: Tariff
    seats: int
    next_seats: int | None
    """Сокращение мест со следующего периода."""
    period: PeriodName
    payment_method: MethodName
    status: SubscriptionStatus
    current_start: date | None
    current_end: date | None
    """Конец оплаченного периода, не включительно."""
    card_amount_kopecks: int | None
    """Сумма автосписания по карте."""


class InvoiceResponse(BaseModel):
    id: UUID
    number: str
    """KR-00042 — его указывают в назначении платежа."""
    kind: InvoiceKind
    status: InvoiceStatus
    payment_method: MethodName
    amount_kopecks: int
    title: str
    purpose: str
    period_start: date | None
    period_end: date | None
    due_date: date | None
    payment_url: str | None
    """Ссылка на оплату картой или СБП (только у неоплаченных)."""
    has_pdf: bool
    """Можно скачать счёт (у оплаты картой счёта нет — будет чек)."""
    created_at: datetime
    paid_at: datetime | None


class ActResponse(BaseModel):
    id: UUID
    number: int
    month: date
    amount_kopecks: int
    created_at: datetime


class BillingResponse(BaseModel):
    """Оплата на странице тарифа. enabled=false — банк не подключён,
    платят по счёту от команды (прежний порядок)."""

    enabled: bool
    tariff: Tariff
    seats: int
    seat_price_kopecks: int | None
    """Цена места в месяц; null — Корпоративный, по договору."""
    quotes: list[QuoteResponse]
    grace_days: int
    subscription: SubscriptionResponse | None
    requisites: RequisitesResponse | None
    invoices: list[InvoiceResponse]
    acts: list[ActResponse]


class SubscriptionChoiceRequest(RequestModel):
    period: PeriodName
    payment_method: MethodName


class SubscriptionChoiceResponse(BaseModel):
    """invoice — счёт или ссылка на первый период; null — подписка уже
    оплачена, выбор действует со следующего счёта."""

    invoice: InvoiceResponse | None


# --- наша панель ----------------------------------------------------------------------


class StaffInvoiceResponse(InvoiceResponse):
    tenant_id: UUID
    company_name: str
    company_code: str
    payer_name: str | None
    payer_inn: str | None


class StaffSubscriptionResponse(SubscriptionResponse):
    tenant_id: UUID
    company_name: str
    company_code: str
    is_active: bool


class StaffBillingResponse(BaseModel):
    enabled: bool
    provider: str
    subscriptions: list[StaffSubscriptionResponse]


class StaffPaymentResponse(BaseModel):
    """Входящий платёж из банка и как он разобран. problem — код причины,
    если не зачёлся сам: amount_differs, inn_differs, already_paid,
    unknown_invoice, no_invoice_number, bank_not_confirmed…"""

    id: UUID
    kind: Literal["incoming", "acquiring"]
    status: PaymentStatus
    problem: str | None
    amount_kopecks: int | None
    payer_inn: str | None
    payer_name: str | None
    purpose: str | None
    tenant_id: UUID | None
    company_name: str | None
    invoice_id: UUID | None
    note: str | None
    created_at: datetime


class StaffPaymentResolveRequest(RequestModel):
    """Зачесть платёж в счёт компании или закрыть без зачёта (только
    комментарий)."""

    tenant_id: UUID | None = None
    invoice_id: UUID | None = None
    note: str | None = Field(default=None, max_length=500)
