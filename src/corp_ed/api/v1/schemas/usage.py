from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from corp_ed.api.v1.schemas.base import RequestModel

OrderStatus = Literal["awaiting_payment", "paid", "cancelled"]
PaymentMethod = Literal["invoice", "card"]


class UsageResponse(BaseModel):
    """Кредиты компании: месячный пул и купленные пакеты."""

    model_config = ConfigDict(from_attributes=True)

    period_start: datetime
    period_end: datetime
    seats: int
    credits_per_seat: int
    pool: int
    used: int
    remaining: int
    """Осталось в месячном пуле."""
    exhausted: bool
    """Месячный пул израсходован (вопросы могут идти из купленных)."""
    warn_at_percent: int
    warning: bool
    """Потрачено не меньше warn_at_percent пула (или пул исчерпан): фронт
    показывает администратору плашку на всех экранах."""
    purchased: int
    """Купленные кредиты, которые ещё не сгорели."""
    purchased_expires_at: datetime | None
    """Когда сгорит ближайшая часть купленных кредитов."""
    purchased_expiring: int
    """Сколько кредитов сгорит в purchased_expires_at."""
    stopped: bool
    """Вопросы остановлены: пул израсходован, купленных нет."""
    avg_credits_per_question: float
    """Сколько в среднем стоит вопрос (BILLING_AVG_CREDITS_PER_QUESTION) —
    для пояснения на странице тарифа."""


class CreditPackResponse(BaseModel):
    code: str
    credits: int
    price_kopecks: int


class CreditOrderRequest(RequestModel):
    pack: str = Field(min_length=1, max_length=32)


class CreditOrderResponse(BaseModel):
    """Заказ пакета кредитов. number — по порядку в компании: «Заказ № 3».
    Оплата пока по счёту (payment_method=invoice)."""

    id: UUID
    number: int
    pack: str
    credits: int
    amount_kopecks: int
    status: OrderStatus
    payment_method: PaymentMethod
    created_at: datetime
    paid_at: datetime | None
    cancelled_at: datetime | None


class StaffCreditOrderResponse(CreditOrderResponse):
    tenant_id: UUID
    company_name: str
    company_code: str


class StaffCreditGrantRequest(RequestModel):
    """Начисление командой без заказа: бонус, компенсация. Комментарий —
    в журнал действий компании."""

    credits: int = Field(ge=1, le=100_000)
    comment: str = Field(min_length=1, max_length=500, pattern=r"\S")


class StaffCreditGrantResponse(BaseModel):
    id: UUID
    credits: int
    expires_at: datetime
