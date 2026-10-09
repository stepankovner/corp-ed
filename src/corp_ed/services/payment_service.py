"""Входящие платежи из банка и наша панель (решения владельца 09.10).

Вебхук: подпись проверена (провайдер) → событие записано (повтор
доставки — та же строка, банк получает 200) → разбор:
- перевод по счёту (incomingPayment): номер KR-… из назначения → счёт
  через invoice_refs → сумма и ИНН плательщика совпадают → статус счёта
  перепроверяется запросом в банк → счёт оплачен, услуга зачислена;
- оплата по ссылке или списание по подписке (acquiringInternetPayment,
  только APPROVED): счёт по operationId → состояние операции в банке →
  каждое списание (approval) зачитывается один раз.

Не сошлось (сумма, ИНН, счёт уже оплачен или отменён, номера нет) —
событие ждёт команду в нашей панели, автоматически ничего не
зачитывается; команде — сообщение без персональных данных. Банк ещё не
подтвердил оплату — событие ждёт, планировщик спросит снова.
"""

import hashlib
from dataclasses import dataclass
from datetime import date, timedelta
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.exceptions import CodedConflictError, NotFoundError
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.billing import PaymentMethod, find_invoice_number
from corp_ed.domain.models import (
    Invoice,
    InvoiceRef,
    PaymentEvent,
    Subscription,
    Tenant,
)
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.services.billing_service import (
    ACTIVE,
    AWAITING,
    PAID,
    Billing,
    IssueSpec,
    admin_email,
    cancel_invoice,
    invoice_notice,
    issue,
    paid_periods,
    renewal_spec,
    requisites_of,
    seats_line,
    settle_invoice,
    subscription_of,
    topup_for,
)
from corp_ed.services.notification_service import NotificationService
from corp_ed.services.payments.provider import (
    BillStatus,
    Line,
    PaymentProviderError,
    WebhookEvent,
)
from corp_ed.services.team_notify import payment_review_message

logger = structlog.get_logger()

MAX_ATTEMPTS = 24
"""Сколько раз спросить банк, прежде чем отдать событие команде: при
опросе раз в 15 минут — около шести часов."""
REVIEW = ("mismatch", "unmatched")
OPEN = ("received", "pending")
EVENTS_LIMIT = 200


@dataclass(frozen=True)
class Outcome:
    status: str
    problem: str | None = None


def _delivery_key(body: bytes, day: date) -> str:
    """Повторная доставка — то же тело в течение нескольких минут. День —
    в ключе: у списаний по подписке тело каждый месяц одинаковое (в нём
    нет даты), а это разные платежи."""
    return hashlib.sha256(day.isoformat().encode() + b"\n" + body).hexdigest()


class PaymentService:
    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        billing: Billing,
    ) -> None:
        self.session_maker = session_maker
        self.billing = billing

    # --- приём ------------------------------------------------------------------------

    async def receive(self, event: WebhookEvent, body: bytes) -> UUID | None:
        """Записать проверенный вебхук. None — разбирать нечего: событие
        другого вида, чужой клиент банка или повтор доставки."""
        if event.kind == "other":
            return None
        provider = self.billing.require()
        expected = getattr(getattr(provider, "settings", None), "customer_code", None)
        if expected and event.customer_code and event.customer_code != expected:
            logger.warning("payment_webhook_foreign_customer")
            return None
        row = PaymentEvent(
            provider=provider.name,
            kind=event.kind,
            delivery_key=_delivery_key(body, self.billing.today()),
            payment_id=event.payment_id,
            amount_kopecks=event.amount_kopecks,
            payer_inn=event.payer_inn,
            payer_name=event.payer_name,
            purpose=event.purpose,
            payment_link_id=event.link_id,
            status="received",
        )
        if event.kind == "acquiring" and event.status != "APPROVED":
            # AUTHORIZED — деньги только заморожены (двухэтапная оплата).
            row.status = "ignored"
            row.problem = "not_approved"
        async with self.session_maker() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                logger.info("payment_webhook_repeated")
                return None
        logger.info("payment_webhook_received", kind=event.kind, event_id=str(row.id))
        return row.id if row.status == "received" else None

    # --- разбор -----------------------------------------------------------------------

    async def process(self, event_id: UUID) -> Outcome | None:
        """Разобрать событие. Ошибка сети банка — событие остаётся
        received/pending, планировщик повторит."""
        async with self.session_maker() as session:
            row = await session.get(PaymentEvent, event_id)
            if row is None or row.status not in OPEN:
                return None
            if row.kind == "incoming":
                number = find_invoice_number(row.purpose)
                ref = (
                    await session.scalar(
                        select(InvoiceRef).where(InvoiceRef.number == number)
                    )
                    if number is not None
                    else None
                )
                refs = [ref] if ref is not None else []
                missing = "no_invoice_number" if number is None else "unknown_invoice"
            else:
                refs = list(
                    (
                        await session.scalars(
                            select(InvoiceRef).where(
                                InvoiceRef.provider_ref == row.payment_id
                            )
                        )
                    ).all()
                )
                missing = "unknown_operation"
        if not refs:
            outcome = await self._finish(event_id, None, Outcome("unmatched", missing))
            if outcome.status in REVIEW:
                await self._notify_team(event_id, None, outcome)
            return outcome
        tenant_id = refs[0].tenant_id
        try:
            with tenant_scope(tenant_id):
                if row.kind == "incoming":
                    outcome = await self._incoming(event_id, tenant_id, refs[0])
                else:
                    outcome = await self._acquiring(event_id, tenant_id)
        except PaymentProviderError as exc:
            logger.warning("payment_recheck_failed", code=exc.code)
            return await self._retry_later(event_id, "bank_unavailable")
        if outcome.status in REVIEW:
            await self._notify_team(event_id, tenant_id, outcome)
        return outcome

    async def _incoming(
        self, event_id: UUID, tenant_id: UUID, ref: InvoiceRef
    ) -> Outcome:
        provider = self.billing.require()
        async with self.session_maker() as session:
            row = await self._lock_event(session, event_id)
            if row is None:
                return Outcome("ignored", "processed")
            row.tenant_id = tenant_id
            row.invoice_id = ref.invoice_id
            row.attempts += 1
            invoice = await session.get(Invoice, ref.invoice_id, with_for_update=True)
            problem = _incoming_problem(row, invoice)
            if problem is not None or invoice is None:
                return await self._close(session, row, Outcome("mismatch", problem))
            if not invoice.provider_ref:
                return await self._close(
                    session, row, Outcome("mismatch", "no_bank_invoice")
                )
            status = await provider.bill_status(invoice.provider_ref)
            if status is BillStatus.PAID:
                row.charge_key = f"in:{row.payment_id}"
                await settle_invoice(
                    session, self.billing, tenant_id, invoice, source="webhook"
                )
                return await self._close(session, row, Outcome("matched"))
            if status is BillStatus.EXPIRED or row.attempts >= MAX_ATTEMPTS:
                return await self._close(
                    session, row, Outcome("mismatch", "bank_not_confirmed")
                )
            return await self._close(
                session, row, Outcome("pending", "bank_not_confirmed")
            )

    async def _acquiring(self, event_id: UUID, tenant_id: UUID) -> Outcome:
        provider = self.billing.require()
        async with self.session_maker() as session:
            row = await self._lock_event(session, event_id)
            if row is None or not row.payment_id:
                return Outcome("ignored", "processed")
            row.tenant_id = tenant_id
            row.attempts += 1
            operation = row.payment_id
            info = await provider.payment_info(operation)
            if not info.approved:
                if row.attempts >= MAX_ATTEMPTS:
                    return await self._close(
                        session, row, Outcome("mismatch", "bank_not_confirmed")
                    )
                return await self._close(
                    session, row, Outcome("pending", "bank_not_confirmed")
                )
            used = set(
                (
                    await session.scalars(
                        select(PaymentEvent.charge_key).where(
                            PaymentEvent.charge_key.like(f"acq:{operation}:%")
                        )
                    )
                ).all()
            )
            fresh = [c for c in info.charges if f"acq:{operation}:{c}" not in used]
            if info.charges and not fresh:
                return await self._close(
                    session, row, Outcome("ignored", "already_counted")
                )
            invoice = await self._invoice_for_charge(session, tenant_id, operation)
            if invoice is None:
                return await self._close(
                    session, row, Outcome("mismatch", "no_awaiting_invoice")
                )
            row.invoice_id = invoice.id
            amount = info.amount_kopecks or row.amount_kopecks
            if amount != invoice.amount_kopecks:
                return await self._close(
                    session, row, Outcome("mismatch", "amount_differs")
                )
            # Номер списания из банка; нет его — один платёж на счёт.
            charge = fresh[0] if fresh else f"inv:{invoice.id}"
            key = f"acq:{operation}:{charge}"
            if key in used:
                return await self._close(
                    session, row, Outcome("ignored", "already_counted")
                )
            row.charge_key = key
            await settle_invoice(
                session, self.billing, tenant_id, invoice, source="webhook"
            )
            return await self._close(session, row, Outcome("matched"))

    async def _invoice_for_charge(
        self, session: AsyncSession, tenant_id: UUID, operation: str
    ) -> Invoice | None:
        """Счёт, который оплачивает это списание: самый старый неоплаченный
        с этой операцией. Нет такого, а это автосписание по подписке и
        пора продлевать — счёт на следующий период выставляется сейчас."""
        invoice = await session.scalar(
            select(Invoice)
            .where(Invoice.provider_ref == operation, Invoice.status == AWAITING)
            .order_by(Invoice.created_at)
            .limit(1)
            .with_for_update()
        )
        if invoice is not None:
            return invoice
        sub = await subscription_of(session, for_update=True)
        if sub is None or sub.card_ref != operation or sub.current_end is None:
            return None
        ahead = timedelta(days=self.billing.settings.invoice_days_ahead)
        if self.billing.today() < sub.current_end - ahead:
            # Списание раньше срока: скорее всего, это повтор уже учтённого.
            return None
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            return None
        spec = renewal_spec(self.billing, sub, tenant, provider_ref=operation)
        return await issue(
            session,
            self.billing,
            tenant_id,
            spec,
            requisites=await requisites_of(session),
            email=None,
            actor_id=None,
        )

    # --- служебное --------------------------------------------------------------------

    @staticmethod
    async def _lock_event(session: AsyncSession, event_id: UUID) -> PaymentEvent | None:
        row = await session.scalar(
            select(PaymentEvent)
            .where(PaymentEvent.id == event_id)
            .with_for_update(skip_locked=True)
        )
        if row is None or row.status not in OPEN:
            return None
        return row

    async def _close(
        self, session: AsyncSession, row: PaymentEvent, outcome: Outcome
    ) -> Outcome:
        row.status = outcome.status
        row.problem = outcome.problem
        row.processed_at = self.billing.now()
        try:
            await session.commit()
        except IntegrityError:
            # Тот же платёж (charge_key) уже зачтён другим событием.
            await session.rollback()
            return await self._finish(
                row.id, None, Outcome("ignored", "already_counted")
            )
        logger.info(
            "payment_event_processed",
            event_id=str(row.id),
            status=outcome.status,
            problem=outcome.problem,
        )
        return outcome

    async def _finish(
        self, event_id: UUID, tenant_id: UUID | None, outcome: Outcome
    ) -> Outcome:
        async with self.session_maker() as session:
            row = await self._lock_event(session, event_id)
            if row is None:
                return outcome
            row.attempts += 1
            return await self._close(session, row, outcome)

    async def _retry_later(self, event_id: UUID, problem: str) -> Outcome:
        async with self.session_maker() as session:
            row = await self._lock_event(session, event_id)
            if row is None:
                return Outcome("ignored", "processed")
            row.attempts += 1
            if row.attempts >= MAX_ATTEMPTS:
                outcome = Outcome("mismatch", problem)
            else:
                outcome = Outcome("pending", problem)
            return await self._close(session, row, outcome)

    async def _notify_team(
        self, event_id: UUID, tenant_id: UUID | None, outcome: Outcome
    ) -> None:
        async with self.session_maker() as session:
            row = await session.get(PaymentEvent, event_id)
            tenant = (
                await session.get(Tenant, row.tenant_id)
                if row is not None and row.tenant_id
                else None
            )
        self.billing.notifier.notify(
            payment_review_message(
                amount_kopecks=row.amount_kopecks if row else None,
                problem=outcome.problem or outcome.status,
                company_code=tenant.company_code if tenant else None,
            )
        )

    async def process_open(self, older_than: timedelta = timedelta(minutes=1)) -> int:
        """Повторить разбор событий, которые ждут: received (процесс упал
        до разбора) и pending (банк ещё не подтвердил)."""
        cutoff = self.billing.now() - older_than
        async with self.session_maker() as session:
            ids = list(
                (
                    await session.scalars(
                        select(PaymentEvent.id)
                        .where(
                            PaymentEvent.status.in_(OPEN),
                            PaymentEvent.created_at < cutoff,
                        )
                        .order_by(PaymentEvent.created_at)
                        .limit(EVENTS_LIMIT)
                    )
                ).all()
            )
        for event_id in ids:
            await self.process(event_id)
        return len(ids)


def _incoming_problem(row: PaymentEvent, invoice: Invoice | None) -> str | None:
    if invoice is None:
        return "unknown_invoice"
    if invoice.status == PAID:
        return "already_paid"
    if invoice.status != AWAITING:
        return "invoice_cancelled"
    if invoice.payment_method != PaymentMethod.INVOICE.value:
        return "wrong_method"
    if row.amount_kopecks != invoice.amount_kopecks:
        return "amount_differs"
    if not row.payer_inn or row.payer_inn != invoice.payer_inn:
        return "inn_differs"
    return None


# --- наша панель ----------------------------------------------------------------------


@dataclass(frozen=True)
class StaffInvoice:
    invoice: Invoice
    tenant: Tenant


@dataclass(frozen=True)
class StaffSubscription:
    subscription: Subscription
    tenant: Tenant


@dataclass(frozen=True)
class SeatsPlan:
    """Что делать с новым числом мест при подключённой оплате.

    defer — сокращение: места меняются со следующего периода (next_seats),
    tenants.seats пока прежнее. topup — добавление: места сразу, доплата
    выставляется. Оба False — как без оплаты: просто поменять."""

    defer: bool = False
    topup: bool = False


class StaffBillingService:
    """Счета, платежи и подписки всех компаний; ручной разбор платежей;
    места при подключённой оплате.

    Как StaffCreditService: таблицы компаний — под RLS, у каждой
    компании своя сессия в её tenant_scope."""

    def __init__(
        self,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
        billing: Billing,
    ) -> None:
        self.session = session
        self.session_maker = session_maker
        self.billing = billing

    async def _tenants(self) -> list[Tenant]:
        return list((await self.session.scalars(select(Tenant))).all())

    async def _tenant(self, tenant_id: UUID) -> Tenant:
        tenant = await self.session.get(Tenant, tenant_id)
        if tenant is None:
            raise NotFoundError("Компания не найдена")
        return tenant

    async def invoices(self, status: str | None) -> list[StaffInvoice]:
        result: list[StaffInvoice] = []
        for tenant in await self._tenants():
            with tenant_scope(tenant.id):
                async with self.session_maker() as session:
                    statement = select(Invoice).order_by(Invoice.created_at.desc())
                    if status is not None:
                        statement = statement.where(Invoice.status == status)
                    rows = (await session.scalars(statement.limit(EVENTS_LIMIT))).all()
            result += [StaffInvoice(invoice=row, tenant=tenant) for row in rows]
        return sorted(result, key=lambda item: item.invoice.created_at, reverse=True)

    async def subscriptions(self) -> list[StaffSubscription]:
        result: list[StaffSubscription] = []
        for tenant in await self._tenants():
            with tenant_scope(tenant.id):
                async with self.session_maker() as session:
                    sub = await subscription_of(session)
            if sub is not None:
                result.append(StaffSubscription(subscription=sub, tenant=tenant))
        order = {"overdue": 0, "awaiting_payment": 1, "active": 2, "cancelled": 3}
        return sorted(result, key=lambda item: order.get(item.subscription.status, 9))

    async def payments(
        self, review_only: bool
    ) -> list[tuple[PaymentEvent, Tenant | None]]:
        statement = select(PaymentEvent).order_by(PaymentEvent.created_at.desc())
        if review_only:
            statement = statement.where(PaymentEvent.status.in_((*REVIEW, "pending")))
        rows = (await self.session.scalars(statement.limit(EVENTS_LIMIT))).all()
        tenants = {tenant.id: tenant for tenant in await self._tenants()}
        return [
            (row, tenants.get(row.tenant_id) if row.tenant_id else None) for row in rows
        ]

    async def resolve(
        self,
        event_id: UUID,
        *,
        staff_account: UUID,
        tenant_id: UUID | None,
        invoice_id: UUID | None,
        note: str | None,
    ) -> PaymentEvent:
        """Команда разобрала платёж: зачесть в указанный счёт (сумма и ИНН
        — на её ответственность) или закрыть без зачёта с комментарием."""
        row = await self.session.get(PaymentEvent, event_id, with_for_update=True)
        if row is None:
            raise NotFoundError("Платёж не найден")
        if row.status in ("matched", "resolved", "ignored"):
            raise CodedConflictError("Платёж уже разобран", "payment_resolved")
        if invoice_id is not None and tenant_id is not None:
            await self.mark_paid(tenant_id, invoice_id, source="staff_payment")
            row.tenant_id = tenant_id
            row.invoice_id = invoice_id
        row.status = "resolved"
        row.note = (note or "").strip()[:500] or None
        row.resolved_by = staff_account
        row.processed_at = self.billing.now()
        await self.session.commit()
        return row

    async def mark_paid(
        self, tenant_id: UUID, invoice_id: UUID, *, source: str = "staff"
    ) -> StaffInvoice:
        tenant = await self._tenant(tenant_id)
        with tenant_scope(tenant.id):
            async with self.session_maker() as session:
                invoice = await session.get(Invoice, invoice_id, with_for_update=True)
                if invoice is None:
                    raise NotFoundError("Счёт не найден")
                if not await settle_invoice(
                    session, self.billing, tenant.id, invoice, source=source
                ):
                    raise CodedConflictError(
                        "Счёт уже оплачен или отменён", "invoice_not_awaiting_payment"
                    )
                await session.commit()
        return StaffInvoice(invoice=invoice, tenant=tenant)

    async def cancel(self, tenant_id: UUID, invoice_id: UUID) -> StaffInvoice:
        tenant = await self._tenant(tenant_id)
        with tenant_scope(tenant.id):
            async with self.session_maker() as session:
                invoice = await session.get(Invoice, invoice_id, with_for_update=True)
                if invoice is None:
                    raise NotFoundError("Счёт не найден")
                if invoice.status != AWAITING:
                    raise CodedConflictError(
                        "Счёт уже оплачен или отменён", "invoice_not_awaiting_payment"
                    )
                await cancel_invoice(session, self.billing, tenant.id, invoice)
                await session.commit()
        return StaffInvoice(invoice=invoice, tenant=tenant)

    # --- места ------------------------------------------------------------------------

    async def plan_seats(self, tenant_id: UUID, current: int, new: int) -> SeatsPlan:
        if not self.billing.enabled or new == current:
            return SeatsPlan()
        with tenant_scope(tenant_id):
            async with self.session_maker() as session:
                sub = await subscription_of(session)
        if sub is None or sub.current_end is None or sub.status == "cancelled":
            return SeatsPlan()
        if self.billing.today() >= sub.current_end:
            # Оплаченный период кончился: места войдут в следующий счёт.
            return SeatsPlan()
        return SeatsPlan(defer=new < current, topup=new > current)

    async def defer_seats(self, tenant_id: UUID, seats: int) -> None:
        """Сокращение со следующего периода: счёт на продление — уже с
        новым числом (неоплаченный выставится заново)."""
        with tenant_scope(tenant_id):
            async with self.session_maker() as session:
                sub = await subscription_of(session, for_update=True)
                if sub is None:
                    return
                sub.next_seats = seats
                await self._drop_renewals(session, tenant_id, sub)
                AuditRepository(session).record(
                    AuditAction.BILLING_SEATS_DEFERRED,
                    tenant_id=tenant_id,
                    target_type="subscription",
                    target_id=sub.id,
                    details={"seats": seats, "from": str(sub.current_end)},
                )
                await session.commit()

    async def issue_topup(self, tenant_id: UUID, added: int) -> Invoice | None:
        """Места уже добавлены (tenants.seats): доплата за added мест по
        оплаченным периодам — счётом или разовой ссылкой, как платит
        компания. added — ровно то, на сколько команда сейчас увеличила
        места: оплаченный наперёд период с другим числом мест на неё не
        влияет."""
        today = self.billing.today()
        with tenant_scope(tenant_id):
            async with self.session_maker() as session:
                sub = await subscription_of(session, for_update=True)
                if sub is None or added <= 0:
                    return None
                # Последнее решение команды — увеличение: сокращение со
                # следующего периода отменяется.
                sub.next_seats = None
                periods = await paid_periods(session, sub, today)
                amount = topup_for(self.billing, periods, added, today)
                await self._drop_renewals(session, tenant_id, sub)
                if amount <= 0:
                    await session.commit()
                    return None
                until = max(p.period_end for p in periods if p.period_end is not None)
                requisites = await requisites_of(session)
                method = PaymentMethod(sub.payment_method)
                tariff = periods[0].tariff or sub.tariff
                email = (requisites.documents_email if requisites else None) or (
                    await admin_email(session, tenant_id)
                )
                invoice = await issue(
                    session,
                    self.billing,
                    tenant_id,
                    IssueSpec(
                        kind="seats",
                        method=method,
                        title=f"Доплата за {added} мест",
                        lines=[
                            Line(
                                name=seats_line(tariff, added, today, until),
                                amount_kopecks=amount,
                            )
                        ],
                        subscription=sub,
                        tariff=tariff,
                        seats=added,
                        due=today
                        + timedelta(days=self.billing.settings.invoice_due_days),
                    ),
                    requisites=requisites,
                    email=email,
                    actor_id=None,
                )
                await NotificationService(session).notify_admins(
                    tenant_id, invoice_notice(invoice)
                )
                await session.commit()
                return invoice

    async def _drop_renewals(
        self, session: AsyncSession, tenant_id: UUID, sub: Subscription
    ) -> None:
        """Неоплаченный счёт на следующий период — со старым числом мест:
        отменить, планировщик выставит новый."""
        if sub.current_end is None:
            return
        rows = (
            await session.scalars(
                select(Invoice)
                .where(
                    Invoice.subscription_id == sub.id,
                    Invoice.kind == "subscription",
                    Invoice.status == AWAITING,
                    Invoice.period_start == sub.current_end,
                )
                .with_for_update()
            )
        ).all()
        for row in rows:
            await cancel_invoice(session, self.billing, tenant_id, row)


def subscription_state(sub: Subscription, today: date, grace_days: int) -> str:
    """Статус по датам: active — оплачено на сегодня; awaiting_payment —
    оплаченный период кончился (или не начинался), льготный срок идёт;
    overdue — льготный срок прошёл."""
    if sub.status == "cancelled":
        return "cancelled"
    if sub.current_end is None:
        return AWAITING
    if today < sub.current_end:
        return ACTIVE
    if today < sub.current_end + timedelta(days=grace_days):
        return AWAITING
    return "overdue"
