"""Платёжный провайдер: что биллингу нужно от банка (решение владельца 09.10).

Счёт юрлицу, его статус и PDF; ссылка на оплату картой или СБП с чеком;
подписка по карте с автосписанием; акт; вебхук о входящем платеже.
Реализация для Точки — tochka.py; в тестах — та же реализация против
поддельного сервера (tests/payments/fake_tochka.py). PAYMENTS_PROVIDER=
none — провайдера нет вовсе, оплату отмечает команда (как раньше).

Суммы — в копейках; в рубли их переводит реализация для своего API.
Позиции — одна строка на услугу с количеством 1 и ценой, равной сумме:
скидка за квартал и год даёт цену места в долях копейки, а банк и касса
проверяют, что цена × количество = сумма.
"""

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any, Literal, Protocol


class PaymentProviderError(Exception):
    """Банк не выполнил запрос. code — для журнала и ответа клиенту;
    retryable — можно повторить позже (сеть, 5xx, 429)."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class InvalidWebhookError(Exception):
    """Подпись вебхука не сошлась или тело не разобрать: обрабатывать
    такой запрос нельзя."""


@dataclass(frozen=True)
class Party:
    """Покупатель — юрлицо или ИП (физлицу счёт не выставить)."""

    name: str
    inn: str
    kpp: str | None
    address: str
    type: Literal["company", "ip"]


@dataclass(frozen=True)
class Line:
    name: str
    amount_kopecks: int


@dataclass(frozen=True)
class BillRequest:
    number: str
    """Номер счёта (KR-00042): банк ищет его в назначении платежа."""
    issued: date
    party: Party
    lines: list[Line]
    total_kopecks: int
    comment: str = ""


@dataclass(frozen=True)
class LinkRequest:
    """Ссылка на оплату с чеком: разовая или подписка по карте."""

    order_id: str
    """Номер заказа у банка (paymentLinkId), уникальный: номер счёта."""
    purpose: str
    email: str
    """Куда касса пришлёт чек."""
    payer_name: str | None
    lines: list[Line]
    total_kopecks: int
    return_url: str | None = None


@dataclass(frozen=True)
class ProviderLink:
    ref: str
    """operationId в банке."""
    url: str


class BillStatus(StrEnum):
    WAITING = "waiting"
    PAID = "paid"
    EXPIRED = "expired"


@dataclass(frozen=True)
class PaymentInfo:
    """Состояние оплаты по ссылке или подписке.

    charges — номера списаний (операции approval): у разовой ссылки одно,
    у подписки по карте — по одному на каждое автосписание."""

    approved: bool
    amount_kopecks: int | None
    charges: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ActRequest:
    number: str
    issued: date
    party: Party
    lines: list[Line]
    total_kopecks: int
    bill_ref: str | None = None
    """Счёт в банке, к которому привязать акт."""


@dataclass(frozen=True)
class WebhookEvent:
    """Разобранный и проверенный вебхук.

    kind: incoming — перевод по реквизитам (счёт), acquiring — оплата по
    ссылке или списание по подписке, other — событие, которое биллингу
    не нужно."""

    kind: Literal["incoming", "acquiring", "other"]
    payment_id: str | None = None
    """paymentId перевода или operationId ссылки."""
    amount_kopecks: int | None = None
    payer_inn: str | None = None
    payer_name: str | None = None
    purpose: str | None = None
    status: str | None = None
    link_id: str | None = None
    """paymentLinkId — номер заказа, переданный при создании ссылки."""
    customer_code: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class PaymentProvider(Protocol):
    name: str

    async def create_bill(self, request: BillRequest) -> str: ...

    async def bill_status(self, ref: str) -> BillStatus: ...

    async def bill_pdf(self, ref: str) -> bytes | None: ...

    async def delete_bill(self, ref: str) -> None: ...

    async def create_link(self, request: LinkRequest) -> ProviderLink: ...

    async def create_card_subscription(self, request: LinkRequest) -> ProviderLink:
        """Подписка по карте: первое списание — по ссылке, дальше банк
        списывает ту же сумму раз в месяц с тем же чеком."""
        ...

    async def cancel_card_subscription(self, ref: str) -> None: ...

    async def payment_info(self, ref: str) -> PaymentInfo: ...

    async def create_act(self, request: ActRequest) -> str | None:
        """Акт в банке; None — банк акты не делает (тогда — свой PDF)."""
        ...

    async def act_pdf(self, ref: str) -> bytes | None: ...

    async def send_act(self, ref: str, email: str) -> None: ...

    def parse_webhook(self, body: bytes) -> WebhookEvent:
        """Проверить подпись и разобрать тело. InvalidWebhookError —
        подпись не сошлась или тело не разобрать."""
        ...
