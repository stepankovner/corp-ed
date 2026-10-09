"""Подписка компании, счета и ссылки на оплату (решения владельца 09.10).

Администратор выбирает период (месяц, квартал, год) и способ оплаты:
- счёт юрлицу — банк выставляет счёт с номером KR-…, оплата приходит
  вебхуком incomingPayment и зачитывается сама (payment_service.py);
- карта — ссылка с чеком (54-ФЗ) и автосписание раз в месяц по графику
  банка (подписка Точки «по графику»; картой — только помесячно).

Период начинается в день оплаты первого счёта; следующий счёт
выставляет планировщик за PAYMENTS_INVOICE_DAYS_AHEAD дней до конца
(billing_scheduler.py). Места добавляет команда — сразу, с доплатой
пропорционально оставшимся дням (по карте — разовой ссылкой: сумму
автосписания банк в середине графика не меняет); сокращение — со
следующего периода. Пакеты кредитов — счётом или ссылкой.

Всё это — только с подключённым банком (PAYMENTS_PROVIDER). Без него
работает прежний ручной поток: заказ ждёт оплаты, команда отмечает.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.config import PaymentSettings, SellerSettings
from corp_ed.core.exceptions import (
    CodedConflictError,
    InvalidBillingInputError,
    NotFoundError,
    PaymentUnavailableError,
)
from corp_ed.core.tenant_context import require_tenant
from corp_ed.domain.billing import (
    BillingPeriod,
    Discounts,
    InvalidRequisitesError,
    PaymentMethod,
    invoice_label,
    invoice_purpose,
    period_amount,
    period_end,
    seat_price,
    topup_amount,
    validate_requisites,
)
from corp_ed.domain.credit_packs import PACK_VALID_MONTHS
from corp_ed.domain.models import (
    Account,
    Act,
    CompanyRequisites,
    CreditOrder,
    Invoice,
    InvoiceRef,
    MemberStatus,
    Subscription,
    Tenant,
    User,
    UserRole,
)
from corp_ed.domain.tariffs import Tariff, plan_for
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.services.billing_pdf import PdfLine, PdfParty, render_act, render_invoice
from corp_ed.services.credit_order_service import settle_order
from corp_ed.services.notification_service import (
    Notice,
    NotificationKind,
    NotificationService,
)
from corp_ed.services.payments.provider import (
    BillRequest,
    Line,
    LinkRequest,
    Party,
    PaymentProvider,
    PaymentProviderError,
)
from corp_ed.services.team_notify import NULL_NOTIFIER, TeamNotifier

logger = structlog.get_logger()

AWAITING = "awaiting_payment"
PAID = "paid"
CANCELLED = "cancelled"
ACTIVE = "active"
OVERDUE = "overdue"

TARIFF_PATH = "/admin/tariff"
INVOICES_LIMIT = 50
ACTS_LIMIT = 24
NUMBER_LOCK = 7_340_221
"""Ключ pg_advisory_xact_lock на выдачу сквозного номера счёта."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def rub(kopecks: int) -> str:
    rubles, rest = divmod(kopecks, 100)
    whole = f"{rubles:,}".replace(",", " ")
    return f"{whole} ₽" if rest == 0 else f"{whole},{rest:02d} ₽"


@dataclass
class Billing:
    """Всё, что нужно биллингу снаружи: настройки, банк, часы."""

    settings: PaymentSettings
    provider: PaymentProvider | None
    seller: SellerSettings
    zone: ZoneInfo
    site_url: str
    notifier: TeamNotifier = NULL_NOTIFIER
    now: Callable[[], datetime] = field(default=_utcnow)

    @property
    def enabled(self) -> bool:
        return self.settings.enabled and self.provider is not None

    @property
    def discounts(self) -> Discounts:
        return Discounts(
            quarter_percent=self.settings.discount_quarter_percent,
            year_percent=self.settings.discount_year_percent,
        )

    def today(self) -> date:
        return self.now().astimezone(self.zone).date()

    def require(self) -> PaymentProvider:
        if not self.enabled or self.provider is None:
            raise CodedConflictError(
                "Оплата через банк пока не подключена — напишите нам, "
                "команда kronto пришлёт счёт",
                "payments_disabled",
            )
        return self.provider

    def return_url(self) -> str:
        return f"{self.site_url}{TARIFF_PATH}"

    def month_start_utc(self, month: date) -> datetime:
        return datetime(month.year, month.month, 1, tzinfo=self.zone).astimezone(UTC)


async def allocate_number(session: AsyncSession) -> int:
    """Следующий сквозной номер счёта. Блокировка — до конца транзакции:
    два счёта разных компаний не получат один номер."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": NUMBER_LOCK}
    )
    current = await session.scalar(
        select(func.coalesce(func.max(InvoiceRef.number), 0))
    )
    return int(current or 0) + 1


def party_of(requisites: CompanyRequisites) -> Party:
    return Party(
        name=requisites.legal_name,
        inn=requisites.inn,
        kpp=requisites.kpp,
        address=requisites.address,
        type="ip" if requisites.payer_type == "ip" else "company",
    )


def invoice_lines(invoice: Invoice | Act) -> list[Line]:
    return [
        Line(name=str(item["name"]), amount_kopecks=int(item["amount_kopecks"]))
        for item in invoice.lines
    ]


def _period_label(period: BillingPeriod, discounts: Discounts) -> str:
    percent = discounts.percent(period)
    discount = f", скидка {float(percent):g} %" if percent else ""
    return {
        BillingPeriod.MONTH: "1 месяц",
        BillingPeriod.QUARTER: f"3 месяца{discount}",
        BillingPeriod.YEAR: f"12 месяцев{discount}",
    }[period]


def _dates(start: date | None, end: date | None) -> str:
    if start is None or end is None:
        return ""
    return f", {start:%d.%m.%Y}–{end - timedelta(days=1):%d.%m.%Y}"


def subscription_line(
    tariff: str,
    seats: int,
    period: BillingPeriod,
    discounts: Discounts,
    start: date | None = None,
    end: date | None = None,
) -> str:
    title = plan_for(tariff).title
    places = plural(seats, "место", "места", "мест")
    return (
        f"Доступ к сервису kronto, тариф «{title}», {seats} {places}, "
        f"{_period_label(period, discounts)}{_dates(start, end)}"
    )


async def admin_email(session: AsyncSession, tenant_id: UUID) -> str | None:
    """Почта первого администратора компании — для чека, когда в
    реквизитах нет почты для документов."""
    email: str | None = await session.scalar(
        select(Account.email)
        .join(User, User.account_id == Account.id)
        .where(
            User.tenant_id == tenant_id,
            User.role == UserRole.ADMIN,
            User.status == MemberStatus.ACTIVE,
        )
        .order_by(User.created_at)
        .limit(1)
    )
    return email


async def requisites_of(session: AsyncSession) -> CompanyRequisites | None:
    return await session.scalar(select(CompanyRequisites))


async def subscription_of(
    session: AsyncSession, *, for_update: bool = False
) -> Subscription | None:
    statement = select(Subscription)
    if for_update:
        statement = statement.with_for_update().execution_options(
            populate_existing=True
        )
    sub: Subscription | None = await session.scalar(statement)
    return sub


@dataclass(frozen=True)
class IssueSpec:
    """Что выставить: вид, способ, позиции и к чему счёт относится."""

    kind: str
    method: PaymentMethod
    title: str
    lines: list[Line]
    recurring: bool = False
    """Карта: подписка с автосписанием, а не разовая ссылка."""
    subscription: Subscription | None = None
    credit_order: CreditOrder | None = None
    tariff: str | None = None
    seats: int | None = None
    period: BillingPeriod | None = None
    period_start: date | None = None
    period_end: date | None = None
    due: date | None = None
    provider_ref: str | None = None
    """Уже известная операция в банке (автосписание по подписке):
    банку ничего не отправляем, ждём списания."""


async def issue(
    session: AsyncSession,
    billing: Billing,
    tenant_id: UUID,
    spec: IssueSpec,
    *,
    requisites: CompanyRequisites | None,
    email: str | None,
    actor_id: UUID | None,
) -> Invoice:
    """Счёт или ссылка: номер, строка в invoices и invoice_refs, запрос в
    банк. Коммит — у вызывающего; ошибка банка — PaymentUnavailableError,
    и транзакция откатывается вместе с номером."""
    provider = billing.require()
    total = sum(line.amount_kopecks for line in spec.lines)
    if total <= 0:
        raise ValueError("invoice total must be positive")
    number = await allocate_number(session)
    today = billing.today()
    invoice = Invoice(
        id=uuid4(),
        number=number,
        kind=spec.kind,
        payment_method=spec.method.value,
        amount_kopecks=total,
        title=spec.title[:300],
        lines=[
            {"name": line.name, "amount_kopecks": line.amount_kopecks}
            for line in spec.lines
        ],
        purpose=invoice_purpose(number, today),
        subscription_id=spec.subscription.id if spec.subscription else None,
        credit_order_id=spec.credit_order.id if spec.credit_order else None,
        tariff=spec.tariff,
        seats=spec.seats,
        period=spec.period.value if spec.period else None,
        period_start=spec.period_start,
        period_end=spec.period_end,
        due_date=spec.due,
        created_by=actor_id,
    )
    if requisites is not None:
        invoice.payer_name = requisites.legal_name
        invoice.payer_inn = requisites.inn
        invoice.payer_kpp = requisites.kpp
        invoice.payer_address = requisites.address
    ref = InvoiceRef(invoice_id=invoice.id, number=number, tenant_id=tenant_id)
    session.add_all([invoice, ref])
    label = invoice_label(number)
    try:
        if spec.provider_ref is not None:
            invoice.provider_ref = spec.provider_ref
        elif spec.method is PaymentMethod.INVOICE:
            if requisites is None:
                raise _requisites_required()
            invoice.provider_ref = await provider.create_bill(
                BillRequest(
                    number=label,
                    issued=today,
                    party=party_of(requisites),
                    lines=spec.lines,
                    total_kopecks=total,
                )
            )
        else:
            if not email:
                raise InvalidBillingInputError(
                    "Укажите почту для документов в реквизитах — на неё придёт чек",
                    code="email_required",
                    field="documents_email",
                )
            request = LinkRequest(
                order_id=label,
                purpose=invoice.purpose,
                email=email,
                payer_name=requisites.legal_name if requisites else None,
                lines=spec.lines,
                total_kopecks=total,
                return_url=billing.return_url(),
            )
            link = await (
                provider.create_card_subscription(request)
                if spec.recurring
                else provider.create_link(request)
            )
            invoice.provider_ref = link.ref
            invoice.payment_url = link.url
    except PaymentProviderError as exc:
        logger.warning(
            "billing_provider_failed", code=exc.code, kind=spec.kind, number=number
        )
        raise PaymentUnavailableError() from exc
    invoice.provider = provider.name
    ref.provider_ref = invoice.provider_ref
    AuditRepository(session).record(
        AuditAction.BILLING_INVOICE_ISSUED,
        tenant_id=tenant_id,
        actor_id=actor_id,
        target_type="invoice",
        target_id=invoice.id,
        details={
            "number": label,
            "kind": spec.kind,
            "payment_method": spec.method.value,
            "amount_kopecks": total,
        },
    )
    await session.flush()
    logger.info(
        "billing_invoice_issued",
        tenant_id=str(tenant_id),
        number=number,
        kind=spec.kind,
        method=spec.method.value,
    )
    return invoice


def _requisites_required() -> InvalidBillingInputError:
    return InvalidBillingInputError(
        "Для счёта нужны реквизиты компании — заполните их в настройках компании",
        code="requisites_required",
    )


async def cancel_invoice(
    session: AsyncSession, billing: Billing, tenant_id: UUID, invoice: Invoice
) -> None:
    """Счёт больше не нужен (сменили выбор, изменились места). Счёт в
    банке удаляется, если получится: иначе банк сопоставит с ним платёж,
    а у нас он отменён — такой платёж уйдёт команде на разбор."""
    invoice.status = CANCELLED
    invoice.cancelled_at = billing.now()
    provider = billing.provider
    ref = invoice.provider_ref
    try:
        if provider is not None and ref:
            if invoice.payment_method == PaymentMethod.INVOICE.value:
                await provider.delete_bill(ref)
            elif invoice.kind == "subscription" and invoice.payment_url:
                # Неоплаченная ссылка на подписку по карте: отменяем график,
                # чтобы по старой ссылке не завелось автосписание.
                await provider.cancel_card_subscription(ref)
    except PaymentProviderError as exc:
        logger.warning("billing_cancel_in_bank_failed", code=exc.code)
    AuditRepository(session).record(
        AuditAction.BILLING_INVOICE_CANCELLED,
        tenant_id=tenant_id,
        target_type="invoice",
        target_id=invoice.id,
        details={"number": invoice_label(invoice.number)},
    )


async def settle_invoice(
    session: AsyncSession,
    billing: Billing,
    tenant_id: UUID,
    invoice: Invoice,
    *,
    source: str,
) -> bool:
    """Счёт оплачен: зачислить то, за что он. Один путь для вебхука,
    сверки с банком и кнопки команды. Вызывать в контексте компании со
    строкой счёта под FOR UPDATE; коммит — у вызывающего. False — счёт
    уже не ждал оплаты (повтор), ничего не сделано."""
    if invoice.status != AWAITING:
        return False
    now = billing.now()
    today = billing.today()
    invoice.status = PAID
    invoice.paid_at = now
    if invoice.kind == "credits" and invoice.credit_order_id is not None:
        order = await session.get(
            CreditOrder, invoice.credit_order_id, with_for_update=True
        )
        if order is not None and order.status == AWAITING:
            await settle_order(session, tenant_id, order, now)
    elif invoice.subscription_id is not None:
        sub = await session.get(
            Subscription, invoice.subscription_id, with_for_update=True
        )
        if sub is not None:
            _apply_to_subscription(billing, sub, invoice, today)
    AuditRepository(session).record(
        AuditAction.BILLING_INVOICE_PAID,
        tenant_id=tenant_id,
        target_type="invoice",
        target_id=invoice.id,
        details={
            "number": invoice_label(invoice.number),
            "amount_kopecks": invoice.amount_kopecks,
            "source": source,
        },
    )
    if invoice.kind != "credits":
        await NotificationService(session).notify_admins(
            tenant_id, paid_notice(invoice)
        )
    logger.info(
        "billing_invoice_paid",
        tenant_id=str(tenant_id),
        number=invoice.number,
        source=source,
    )
    return True


def _apply_to_subscription(
    billing: Billing, sub: Subscription, invoice: Invoice, today: date
) -> None:
    if invoice.kind == "seats":
        sub.seats += invoice.seats or 0
        return
    if invoice.kind != "subscription":
        return
    period = BillingPeriod(invoice.period or sub.period)
    if invoice.period_start is None:
        # Первый счёт: период начинается в день оплаты.
        invoice.period_start = today
        invoice.period_end = period_end(today, period)
        title = invoice.lines[0]["name"] if invoice.lines else ""
        if title and invoice.seats and invoice.tariff:
            invoice.lines = [
                {
                    **invoice.lines[0],
                    "name": subscription_line(
                        invoice.tariff,
                        invoice.seats,
                        period,
                        billing.discounts,
                        invoice.period_start,
                        invoice.period_end,
                    ),
                },
                *invoice.lines[1:],
            ]
    if sub.current_end is None or (
        invoice.period_end is not None and invoice.period_end > sub.current_end
    ):
        sub.current_start = invoice.period_start
        sub.current_end = invoice.period_end
    if invoice.seats:
        sub.seats = invoice.seats
    if invoice.tariff:
        sub.tariff = invoice.tariff
    if invoice.payment_method == PaymentMethod.CARD.value and invoice.provider_ref:
        sub.card_ref = invoice.provider_ref
        sub.card_amount_kopecks = invoice.amount_kopecks
    if sub.current_end is not None and today < sub.current_end:
        sub.status = ACTIVE
    sub.overdue_notified_for = None


def paid_notice(invoice: Invoice) -> Notice:
    label = invoice_label(invoice.number)
    lines = [f"Счёт {label} на {rub(invoice.amount_kopecks)} оплачен."]
    if invoice.kind == "subscription" and invoice.period_end is not None:
        last = invoice.period_end - timedelta(days=1)
        lines.append(f"Доступ оплачен по {last:%d.%m.%Y} включительно.")
    elif invoice.kind == "seats":
        lines.append("Добавленные места оплачены до конца периода.")
    lines.append("Акт за месяц придёт в начале следующего месяца.")
    return Notice(
        kind=NotificationKind.BILLING_PAID,
        title="Оплата получена",
        lines=lines,
        link=TARIFF_PATH,
        action="Открыть тариф",
    )


def invoice_notice(invoice: Invoice, *, reminder: bool = False) -> Notice:
    label = invoice_label(invoice.number)
    amount = rub(invoice.amount_kopecks)
    if invoice.payment_method == PaymentMethod.CARD.value and invoice.payment_url:
        lines = [f"Ссылка на оплату картой или СБП на {amount} — на странице тарифа."]
    elif invoice.payment_method == PaymentMethod.CARD.value:
        lines = [f"Банк спишет {amount} с привязанной карты по графику."]
    else:
        lines = [
            f"Счёт {label} на {amount} — на странице тарифа, его можно скачать.",
            f"В назначении платежа укажите номер {label}: тогда оплата "
            "зачтётся автоматически.",
        ]
    payable = invoice.payment_url is not None or invoice.payment_method == "invoice"
    if invoice.due_date is not None and payable:
        lines.append(f"Оплатить до {invoice.due_date:%d.%m.%Y}.")
    return Notice(
        kind=NotificationKind.BILLING_INVOICE,
        title=("Напоминание: " if reminder else "") + f"счёт {label} ждёт оплаты",
        lines=lines,
        link=TARIFF_PATH,
        action="Открыть тариф",
    )


# --- реквизиты ------------------------------------------------------------------------


@dataclass(frozen=True)
class RequisitesInput:
    legal_name: str
    inn: str
    kpp: str | None
    address: str
    documents_email: str | None


class RequisitesService:
    """Реквизиты компании для счёта и акта — вводит администратор."""

    def __init__(self, session: AsyncSession, audit: AuditRepository) -> None:
        self.session = session
        self.audit = audit

    async def get(self) -> CompanyRequisites | None:
        return await requisites_of(self.session)

    async def save(self, admin: User, data: RequisitesInput) -> CompanyRequisites:
        try:
            payer_type, kpp = validate_requisites(inn=data.inn, kpp=data.kpp)
        except InvalidRequisitesError as exc:
            raise InvalidBillingInputError(
                exc.message, code="invalid_requisites", field=exc.field
            ) from exc
        tenant_id = require_tenant()
        requisites = await requisites_of(self.session)
        if requisites is None:
            requisites = CompanyRequisites()
            self.session.add(requisites)
        requisites.legal_name = data.legal_name.strip()
        requisites.inn = data.inn.strip()
        requisites.kpp = kpp
        requisites.payer_type = payer_type
        requisites.address = data.address.strip()
        requisites.documents_email = (data.documents_email or "").strip() or None
        requisites.updated_by = admin.id
        self.audit.record(
            AuditAction.BILLING_REQUISITES_UPDATED,
            tenant_id=tenant_id,
            actor_id=admin.id,
            target_type="company_requisites",
            details={"inn": requisites.inn, "payer_type": payer_type},
        )
        await self.session.commit()
        await self.session.refresh(requisites)
        return requisites


# --- администратор компании -----------------------------------------------------------


@dataclass(frozen=True)
class Quote:
    period: BillingPeriod
    discount_percent: float
    amount_kopecks: int
    """За период: места × цена × месяцы − скидка."""


@dataclass(frozen=True)
class BillingOverview:
    enabled: bool
    tariff: str
    seats: int
    seat_price_kopecks: int | None
    quotes: list[Quote]
    subscription: Subscription | None
    requisites: CompanyRequisites | None
    invoices: list[Invoice]
    acts: list[Act]
    grace_days: int


def _quotes(billing: Billing, tariff: str, seats: int) -> list[Quote]:
    if Tariff(tariff) not in (Tariff.BASE, Tariff.EXTENDED):
        return []
    return [
        Quote(
            period=period,
            discount_percent=float(billing.discounts.percent(period)),
            amount_kopecks=period_amount(tariff, seats, period, billing.discounts),
        )
        for period in BillingPeriod
    ]


class BillingService:
    """Страница тарифа глазами администратора компании (тенант из токена)."""

    def __init__(
        self, session: AsyncSession, billing: Billing, audit: AuditRepository
    ) -> None:
        self.session = session
        self.billing = billing
        self.audit = audit

    async def overview(self) -> BillingOverview:
        tenant = await self._tenant()
        invoices = (
            await self.session.scalars(
                select(Invoice)
                .order_by(Invoice.created_at.desc(), Invoice.number.desc())
                .limit(INVOICES_LIMIT)
            )
        ).all()
        acts = (
            await self.session.scalars(
                select(Act).order_by(Act.month.desc()).limit(ACTS_LIMIT)
            )
        ).all()
        seats_price = (
            seat_price(tenant.tariff)
            if Tariff(tenant.tariff) in (Tariff.BASE, Tariff.EXTENDED)
            else None
        )
        return BillingOverview(
            enabled=self.billing.enabled,
            tariff=tenant.tariff,
            seats=tenant.seats,
            seat_price_kopecks=seats_price,
            quotes=_quotes(self.billing, tenant.tariff, tenant.seats),
            subscription=await subscription_of(self.session),
            requisites=await requisites_of(self.session),
            invoices=list(invoices),
            acts=list(acts),
            grace_days=self.billing.settings.grace_days,
        )

    async def choose(
        self, admin: User, period: BillingPeriod, method: PaymentMethod
    ) -> Invoice | None:
        """Выбор периода и способа оплаты. Подписки ещё нет или она ни разу
        не оплачена — сразу счёт или ссылка на первый период. Оплаченная —
        выбор действует со следующего счёта (None: сейчас платить нечего)."""
        self.billing.require()
        tenant_id = require_tenant()
        tenant = await TenantRepository(self.session).lock(tenant_id)
        if Tariff(tenant.tariff) not in (Tariff.BASE, Tariff.EXTENDED):
            raise CodedConflictError(
                "Корпоративный тариф оплачивается по договору — счёт пришлёт "
                "команда kronto",
                "tariff_by_contract",
            )
        if method is PaymentMethod.CARD and period is not BillingPeriod.MONTH:
            raise InvalidBillingInputError(
                "Картой — только помесячно; за квартал и год — счётом",
                code="card_monthly_only",
                field="period",
            )
        requisites = await requisites_of(self.session)
        if method is PaymentMethod.INVOICE and requisites is None:
            raise _requisites_required()
        sub = await subscription_of(self.session, for_update=True)
        if sub is None:
            sub = Subscription(
                tariff=tenant.tariff,
                seats=tenant.seats,
                period=period.value,
                payment_method=method.value,
                status=AWAITING,
                created_by=admin.id,
            )
            self.session.add(sub)
            await self.session.flush()
        previous_method = sub.payment_method
        sub.period = period.value
        sub.payment_method = method.value
        # Неоплаченный счёт на подписку больше не нужен: выбор другой.
        for pending in await self._awaiting(sub, "subscription"):
            await cancel_invoice(self.session, self.billing, tenant_id, pending)
        invoice: Invoice | None = None
        if sub.current_end is None or sub.status == CANCELLED:
            sub.status = AWAITING
            invoice = await issue(
                self.session,
                self.billing,
                tenant_id,
                self._first_spec(sub, tenant, period, method),
                requisites=requisites,
                email=self._email(requisites, admin),
                actor_id=admin.id,
            )
        elif previous_method == PaymentMethod.CARD.value and method is not (
            PaymentMethod.CARD
        ):
            # Перешли с карты на счёт: автосписание по графику отключаем,
            # текущий период уже оплачен.
            await self._cancel_card(sub)
        self.audit.record(
            AuditAction.BILLING_SUBSCRIPTION_CHOSEN,
            tenant_id=tenant_id,
            actor_id=admin.id,
            target_type="subscription",
            target_id=sub.id,
            details={"period": period.value, "payment_method": method.value},
        )
        await self.session.commit()
        return invoice

    def _first_spec(
        self,
        sub: Subscription,
        tenant: Tenant,
        period: BillingPeriod,
        method: PaymentMethod,
    ) -> IssueSpec:
        amount = period_amount(
            tenant.tariff, tenant.seats, period, self.billing.discounts
        )
        name = subscription_line(
            tenant.tariff, tenant.seats, period, self.billing.discounts
        )
        return IssueSpec(
            kind="subscription",
            method=method,
            title=f"Подписка kronto, тариф «{plan_for(tenant.tariff).title}»",
            lines=[Line(name=name, amount_kopecks=amount)],
            recurring=method is PaymentMethod.CARD,
            subscription=sub,
            tariff=tenant.tariff,
            seats=tenant.seats,
            period=period,
            due=self.billing.today()
            + timedelta(days=self.billing.settings.invoice_due_days),
        )

    async def _cancel_card(self, sub: Subscription) -> None:
        provider = self.billing.provider
        if sub.card_ref and provider is not None:
            try:
                await provider.cancel_card_subscription(sub.card_ref)
            except PaymentProviderError as exc:
                logger.warning("billing_card_cancel_failed", code=exc.code)
        sub.card_ref = None
        sub.card_amount_kopecks = None

    async def _awaiting(self, sub: Subscription, kind: str) -> list[Invoice]:
        return list(
            (
                await self.session.scalars(
                    select(Invoice)
                    .where(
                        Invoice.subscription_id == sub.id,
                        Invoice.kind == kind,
                        Invoice.status == AWAITING,
                    )
                    .with_for_update()
                )
            ).all()
        )

    @staticmethod
    def _email(requisites: CompanyRequisites | None, admin: User) -> str | None:
        if requisites is not None and requisites.documents_email:
            return requisites.documents_email
        return admin.account.email if admin.account is not None else None

    async def invoice_pdf(self, invoice_id: UUID) -> tuple[Invoice, bytes]:
        invoice = await self.session.get(Invoice, invoice_id)
        if invoice is None:
            raise NotFoundError("Счёт не найден")
        if invoice.payment_method != PaymentMethod.INVOICE.value:
            raise NotFoundError("У оплаты картой нет счёта — чек пришлёт касса")
        provider = self.billing.provider
        if provider is not None and invoice.provider_ref:
            try:
                pdf = await provider.bill_pdf(invoice.provider_ref)
            except PaymentProviderError as exc:
                logger.warning("billing_bill_pdf_failed", code=exc.code)
                pdf = None
            if pdf is not None:
                return invoice, pdf
        tenant = await self._tenant()
        return invoice, render_invoice(
            font_path=self.billing.settings.pdf_font,
            seller=self.billing.seller,
            number=invoice_label(invoice.number),
            issued=invoice.created_at.astimezone(self.billing.zone).date(),
            buyer=_pdf_party(invoice, tenant),
            lines=[
                PdfLine(line.name, line.amount_kopecks)
                for line in invoice_lines(invoice)
            ],
            total_kopecks=invoice.amount_kopecks,
            purpose=invoice.purpose,
            due=invoice.due_date,
        )

    async def act_pdf(self, act_id: UUID) -> tuple[Act, bytes]:
        act = await self.session.get(Act, act_id)
        if act is None:
            raise NotFoundError("Акт не найден")
        provider = self.billing.provider
        if provider is not None and act.provider_ref:
            try:
                pdf = await provider.act_pdf(act.provider_ref)
            except PaymentProviderError as exc:
                logger.warning("billing_act_pdf_failed", code=exc.code)
                pdf = None
            if pdf is not None:
                return act, pdf
        tenant = await self._tenant()
        return act, render_act(
            font_path=self.billing.settings.pdf_font,
            seller=self.billing.seller,
            number=str(act.number),
            issued=act.created_at.astimezone(self.billing.zone).date(),
            month=act.month,
            buyer=_pdf_party(act, tenant),
            lines=[
                PdfLine(line.name, line.amount_kopecks) for line in invoice_lines(act)
            ],
            total_kopecks=act.amount_kopecks,
        )

    async def _tenant(self) -> Tenant:
        tenant = await self.session.get(Tenant, require_tenant())
        if tenant is None:
            raise NotFoundError("Компания не найдена")
        return tenant


def _pdf_party(document: Invoice | Act, tenant: Tenant) -> PdfParty:
    return PdfParty(
        name=document.payer_name or tenant.name,
        inn=document.payer_inn,
        kpp=document.payer_kpp,
        address=document.payer_address,
    )


class BankInvoiceIssuer:
    """Счёт или ссылка на пакет кредитов (CreditOrderService.invoices)."""

    def __init__(self, billing: Billing) -> None:
        self.billing = billing

    async def issue(
        self, session: AsyncSession, order: CreditOrder, admin: User
    ) -> Invoice | None:
        method = PaymentMethod(order.payment_method)
        requisites = await requisites_of(session)
        if method is PaymentMethod.INVOICE and requisites is None:
            raise _requisites_required()
        months = plural(PACK_VALID_MONTHS, "месяц", "месяца", "месяцев")
        name = (
            f"Пакет {order.credits:,} кредитов kronto (действуют "
            f"{PACK_VALID_MONTHS} {months} с зачисления)"
        ).replace(",", " ")
        return await issue(
            session,
            self.billing,
            require_tenant(),
            IssueSpec(
                kind="credits",
                method=method,
                title=f"Пакет кредитов, заказ № {order.number}",
                lines=[Line(name=name, amount_kopecks=order.amount_kopecks)],
                credit_order=order,
                due=self.billing.today()
                + timedelta(days=self.billing.settings.invoice_due_days),
            ),
            requisites=requisites,
            email=BillingService._email(requisites, admin),
            actor_id=admin.id,
        )


def renewal_spec(
    billing: Billing,
    sub: Subscription,
    tenant: Tenant,
    *,
    provider_ref: str | None = None,
) -> IssueSpec:
    """Счёт на следующий период: с конца оплаченного, места — со
    сокращением, если оно запланировано, тариф — нынешний тариф компании.
    Срок оплаты — первый день нового периода."""
    assert sub.current_end is not None  # noqa: S101 — продлевается оплаченное
    method = PaymentMethod(sub.payment_method)
    period = (
        BillingPeriod.MONTH
        if method is PaymentMethod.CARD
        else BillingPeriod(sub.period)
    )
    tariff = (
        tenant.tariff
        if Tariff(tenant.tariff) in (Tariff.BASE, Tariff.EXTENDED)
        else sub.tariff
    )
    seats = sub.next_seats or tenant.seats
    start = sub.current_end
    end = period_end(start, period)
    amount = period_amount(tariff, seats, period, billing.discounts)
    return IssueSpec(
        kind="subscription",
        method=method,
        title=f"Подписка kronto, тариф «{plan_for(tariff).title}»",
        lines=[
            Line(
                name=subscription_line(
                    tariff, seats, period, billing.discounts, start, end
                ),
                amount_kopecks=amount,
            )
        ],
        recurring=method is PaymentMethod.CARD and provider_ref is None,
        subscription=sub,
        tariff=tariff,
        seats=seats,
        period=period,
        period_start=start,
        period_end=end,
        due=start,
        provider_ref=provider_ref,
    )


# --- места (команда) ------------------------------------------------------------------


async def paid_periods(
    session: AsyncSession, sub: Subscription, today: date
) -> list[Invoice]:
    """Оплаченные периоды подписки, которые ещё не кончились."""
    return list(
        (
            await session.scalars(
                select(Invoice).where(
                    Invoice.subscription_id == sub.id,
                    Invoice.kind == "subscription",
                    Invoice.status == PAID,
                    Invoice.period_end > today,
                )
            )
        ).all()
    )


def topup_for(billing: Billing, periods: list[Invoice], added: int, today: date) -> int:
    """Доплата за added мест по всем оплаченным и ещё идущим периодам:
    текущий — пропорционально оставшимся дням, оплаченный наперёд —
    целиком."""
    total = 0
    for item in periods:
        if item.period_start is None or item.period_end is None:
            continue
        total += topup_amount(
            item.tariff or Tariff.BASE,
            added,
            BillingPeriod(item.period or BillingPeriod.MONTH),
            billing.discounts,
            start=item.period_start,
            end=item.period_end,
            today=today,
        )
    return total


def seats_line(tariff: str, added: int, today: date, until: date) -> str:
    places = plural(added, "место", "места", "мест")
    return (
        f"Доплата за {added} {places} тарифа «{plan_for(tariff).title}» "
        f"с {today:%d.%m.%Y} по {until - timedelta(days=1):%d.%m.%Y}"
    )
