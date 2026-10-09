"""Планировщик оплаты в воркере (решения владельца 09.10).

Раз в 15 минут, только с подключённым банком:
- разбор платежей, которые ждут (банк ещё не подтвердил, процесс упал);
- сверка неоплаченных счетов и ссылок с банком — на случай, если вебхук
  не дошёл (банк шлёт их только об успешных операциях и не всегда);
- счёт на следующий период за PAYMENTS_INVOICE_DAYS_AHEAD дней до конца
  оплаченного, напоминание в день окончания;
- статус подписки: оплаченный период кончился — «ждёт оплаты», прошёл
  льготный срок (PAYMENTS_GRACE_DAYS) — «просрочена»: команде сообщение
  один раз за период, администраторам — уведомление. Компанию никто не
  блокирует: решение за командой (кнопка «Приостановить» в панели);
- сокращение мест — когда начинается период, за который оно оплачено;
- акт за прошедший месяц — по оплаченным в нём счетам.

Всё идемпотентно, как недельная сводка: счёт на период — уникальный
индекс (subscription_id, period_start), акт — (tenant_id, month),
сообщение о просрочке — отметка overdue_notified_for. Два воркера не
выставят второй счёт и не пришлют второго письма.
"""

import contextlib
from dataclasses import dataclass, replace
from datetime import date, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.exceptions import (
    InvalidBillingInputError,
    PaymentUnavailableError,
)
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.billing import PaymentMethod
from corp_ed.domain.models import (
    Account,
    Act,
    Invoice,
    MemberStatus,
    PaymentEvent,
    Subscription,
    Tenant,
    User,
    UserRole,
)
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.services.billing_pdf import month_name
from corp_ed.services.billing_service import (
    AWAITING,
    PAID,
    TARIFF_PATH,
    Billing,
    invoice_lines,
    invoice_notice,
    issue,
    party_of,
    renewal_spec,
    requisites_of,
    settle_invoice,
    subscription_of,
)
from corp_ed.services.notification_service import (
    Notice,
    NotificationKind,
    NotificationService,
)
from corp_ed.services.payment_service import PaymentService, subscription_state
from corp_ed.services.payments.provider import (
    ActRequest,
    Line,
    PaymentProviderError,
)
from corp_ed.services.team_notify import billing_overdue_message

logger = structlog.get_logger()

POLL_AFTER = timedelta(minutes=5)
"""Сверять с банком счёт не раньше: оплату обычно приносит вебхук."""
POLL_WITHIN = timedelta(days=45)
"""Старше — не сверять каждые 15 минут: такой счёт разбирает команда."""


@dataclass
class SchedulerReport:
    invoices: int = 0
    settled: int = 0
    acts: int = 0
    overdue: int = 0


class BillingScheduler:
    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        billing: Billing,
        payments: PaymentService,
    ) -> None:
        self.session_maker = session_maker
        self.billing = billing
        self.payments = payments

    async def run_due(self) -> SchedulerReport:
        report = SchedulerReport()
        if not self.billing.enabled:
            return report
        await self.payments.process_open()
        async with self.session_maker() as session:
            tenants = await TenantRepository(session).list_all()
        for tenant in tenants:
            try:
                with tenant_scope(tenant.id):
                    await self._tenant(tenant, report)
            except Exception:
                logger.exception("billing_tenant_tick_failed", tenant_id=str(tenant.id))
        if report.invoices or report.settled or report.acts or report.overdue:
            logger.info("billing_scheduled", **report.__dict__)
        return report

    async def _tenant(self, tenant: Tenant, report: SchedulerReport) -> None:
        today = self.billing.today()
        async with self.session_maker() as session:
            report.settled += await self._poll(session, tenant)
        async with self.session_maker() as session:
            sub = await subscription_of(session, for_update=True)
            if sub is not None and sub.current_end is not None:
                await self._apply_next_seats(session, tenant, sub, today)
                report.invoices += await self._renew(session, tenant, sub, today)
                report.overdue += await self._status(session, tenant, sub, today)
            await session.commit()
        async with self.session_maker() as session:
            report.acts += await self._act(session, tenant, today)

    # --- сверка с банком --------------------------------------------------------------

    async def _poll(self, session: AsyncSession, tenant: Tenant) -> int:
        provider = self.billing.require()
        now = self.billing.now()
        rows = (
            await session.scalars(
                select(Invoice).where(
                    Invoice.status == AWAITING,
                    Invoice.provider_ref.is_not(None),
                    Invoice.created_at < now - POLL_AFTER,
                    Invoice.created_at > now - POLL_WITHIN,
                )
            )
        ).all()
        settled = 0
        for row in rows:
            ref = row.provider_ref or ""
            try:
                if row.payment_method == PaymentMethod.INVOICE.value:
                    paid = (await provider.bill_status(ref)).value == "paid"
                    key = f"bill:{row.id}"
                else:
                    info = await provider.payment_info(ref)
                    used = set(
                        (
                            await session.scalars(
                                select(PaymentEvent.charge_key).where(
                                    PaymentEvent.charge_key.like(f"acq:{ref}:%")
                                )
                            )
                        ).all()
                    )
                    fresh = [c for c in info.charges if f"acq:{ref}:{c}" not in used]
                    paid = info.approved and (
                        bool(fresh)
                        or (not info.charges and row.payment_url is not None)
                    )
                    key = f"acq:{ref}:{fresh[0] if fresh else f'inv:{row.id}'}"
                    if key in used:
                        paid = False
            except PaymentProviderError as exc:
                logger.warning("billing_poll_failed", code=exc.code)
                continue
            if not paid:
                continue
            invoice = await session.get(Invoice, row.id, with_for_update=True)
            if invoice is None or invoice.status != AWAITING:
                continue
            # Платёж, найденный сверкой, — тоже событие: вебхук, если
            # всё-таки придёт, увидит, что списание уже зачтено.
            session.add(
                PaymentEvent(
                    provider=provider.name,
                    kind="incoming"
                    if invoice.payment_method == PaymentMethod.INVOICE.value
                    else "acquiring",
                    delivery_key=f"poll:{invoice.id}",
                    charge_key=key,
                    payment_id=ref,
                    amount_kopecks=invoice.amount_kopecks,
                    payer_inn=invoice.payer_inn,
                    payer_name=invoice.payer_name,
                    status="matched",
                    problem="found_by_reconciliation",
                    tenant_id=tenant.id,
                    invoice_id=invoice.id,
                    processed_at=now,
                )
            )
            await settle_invoice(
                session, self.billing, tenant.id, invoice, source="reconciliation"
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                continue
            settled += 1
        return settled

    # --- подписка ---------------------------------------------------------------------

    async def _apply_next_seats(
        self, session: AsyncSession, tenant: Tenant, sub: Subscription, today: date
    ) -> None:
        if (
            sub.next_seats is None
            or sub.current_start is None
            or today < sub.current_start
            or sub.seats != sub.next_seats
        ):
            return
        locked = await TenantRepository(session).lock(tenant.id)
        previous = locked.seats
        locked.seats = sub.next_seats
        sub.next_seats = None
        AuditRepository(session).record(
            AuditAction.BILLING_SEATS_APPLIED,
            tenant_id=tenant.id,
            target_type="tenant",
            target_id=tenant.id,
            details={"from": previous, "to": locked.seats},
        )

    async def _renew(
        self, session: AsyncSession, tenant: Tenant, sub: Subscription, today: date
    ) -> int:
        assert sub.current_end is not None  # noqa: S101 — проверено выше
        ahead = timedelta(days=self.billing.settings.invoice_days_ahead)
        if sub.status == "cancelled" or today < sub.current_end - ahead:
            return 0
        exists = await session.scalar(
            select(func.count()).where(
                Invoice.subscription_id == sub.id,
                Invoice.kind == "subscription",
                Invoice.period_start == sub.current_end,
                Invoice.status != "cancelled",
            )
        )
        if exists:
            return 0
        requisites = await requisites_of(session)
        method = PaymentMethod(sub.payment_method)
        audit = AuditRepository(session)
        if method is PaymentMethod.INVOICE and requisites is None:
            since = self.billing.month_start_utc(sub.current_end - ahead)
            if not await audit.exists_since(
                tenant.id, AuditAction.BILLING_REQUISITES_MISSING, since
            ):
                audit.record(
                    AuditAction.BILLING_REQUISITES_MISSING, tenant_id=tenant.id
                )
                await NotificationService(session).notify_admins(
                    tenant.id, requisites_notice(sub.current_end)
                )
            return 0
        spec = renewal_spec(self.billing, sub, tenant)
        amount = sum(line.amount_kopecks for line in spec.lines)
        email = (requisites.documents_email if requisites else None) or (
            await admin_email(session, tenant.id)
        )
        if method is PaymentMethod.CARD:
            if sub.card_ref and sub.card_amount_kopecks == amount:
                # Сумма та же — банк спишет по графику, ждём списания.
                spec = replace(spec, provider_ref=sub.card_ref, recurring=False)
            else:
                # Сумма изменилась (места, тариф): график банка с той же
                # суммой отменяется, нужна новая подписка — по ссылке.
                await self._cancel_card(sub)
        try:
            async with session.begin_nested():
                invoice = await issue(
                    session,
                    self.billing,
                    tenant.id,
                    spec,
                    requisites=requisites,
                    email=email,
                    actor_id=None,
                )
        except (PaymentUnavailableError, InvalidBillingInputError) as exc:
            logger.warning("billing_renewal_failed", error=type(exc).__name__)
            return 0
        except IntegrityError:
            return 0
        await NotificationService(session).notify_admins(
            tenant.id, invoice_notice(invoice)
        )
        return 1

    async def _cancel_card(self, sub: Subscription) -> None:
        provider = self.billing.provider
        if sub.card_ref and provider is not None:
            with contextlib.suppress(PaymentProviderError):
                await provider.cancel_card_subscription(sub.card_ref)
        sub.card_ref = None
        sub.card_amount_kopecks = None

    async def _status(
        self, session: AsyncSession, tenant: Tenant, sub: Subscription, today: date
    ) -> int:
        assert sub.current_end is not None  # noqa: S101
        state = subscription_state(sub, today, self.billing.settings.grace_days)
        sub.status = state
        if state == AWAITING and today >= sub.current_end:
            renewal = await session.scalar(
                select(Invoice)
                .where(
                    Invoice.subscription_id == sub.id,
                    Invoice.kind == "subscription",
                    Invoice.period_start == sub.current_end,
                    Invoice.status == AWAITING,
                    Invoice.reminded_at.is_(None),
                )
                .with_for_update()
            )
            if renewal is not None:
                renewal.reminded_at = self.billing.now()
                await NotificationService(session).notify_admins(
                    tenant.id, invoice_notice(renewal, reminder=True)
                )
        if state != "overdue" or sub.overdue_notified_for == sub.current_end:
            return 0
        sub.overdue_notified_for = sub.current_end
        AuditRepository(session).record(
            AuditAction.BILLING_OVERDUE,
            tenant_id=tenant.id,
            target_type="subscription",
            target_id=sub.id,
            details={"paid_until": sub.current_end.isoformat()},
        )
        await NotificationService(session).notify_admins(
            tenant.id, overdue_notice(sub.current_end)
        )
        self.billing.notifier.notify(
            billing_overdue_message(
                company_code=tenant.company_code,
                paid_until=sub.current_end - timedelta(days=1),
            )
        )
        return 1

    # --- акты -------------------------------------------------------------------------

    async def _act(self, session: AsyncSession, tenant: Tenant, today: date) -> int:
        month = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        if await session.scalar(select(Act.id).where(Act.month == month)):
            return 0
        start = self.billing.month_start_utc(month)
        end = self.billing.month_start_utc(today.replace(day=1))
        invoices = list(
            (
                await session.scalars(
                    select(Invoice)
                    .where(
                        Invoice.status == PAID,
                        Invoice.paid_at >= start,
                        Invoice.paid_at < end,
                    )
                    .order_by(Invoice.paid_at)
                )
            ).all()
        )
        if not invoices:
            return 0
        lines: list[Line] = [line for item in invoices for line in invoice_lines(item)]
        total = sum(line.amount_kopecks for line in lines)
        number = (
            int(
                await session.scalar(select(func.coalesce(func.max(Act.number), 0)))
                or 0
            )
            + 1
        )
        requisites = await requisites_of(session)
        act = Act(
            number=number,
            month=month,
            amount_kopecks=total,
            lines=[
                {"name": line.name, "amount_kopecks": line.amount_kopecks}
                for line in lines
            ],
            invoice_ids=[item.id for item in invoices],
        )
        if requisites is not None:
            act.payer_name = requisites.legal_name
            act.payer_inn = requisites.inn
            act.payer_kpp = requisites.kpp
            act.payer_address = requisites.address
        else:
            first = invoices[0]
            act.payer_name = first.payer_name or tenant.name
            act.payer_inn = first.payer_inn
            act.payer_kpp = first.payer_kpp
            act.payer_address = first.payer_address
        provider = self.billing.provider
        if provider is not None and requisites is not None:
            bank_bills = [
                item.provider_ref
                for item in invoices
                if item.payment_method == PaymentMethod.INVOICE.value
                and item.provider_ref
            ]
            try:
                act.provider_ref = await provider.create_act(
                    ActRequest(
                        number=str(number),
                        issued=today,
                        party=party_of(requisites),
                        lines=lines,
                        total_kopecks=total,
                        bill_ref=bank_bills[0] if len(bank_bills) == 1 else None,
                    )
                )
                act.provider = provider.name if act.provider_ref else None
                if act.provider_ref and requisites.documents_email:
                    await provider.send_act(
                        act.provider_ref, requisites.documents_email
                    )
            except PaymentProviderError as exc:
                # Акт банка не вышел — будет свой PDF на странице тарифа.
                logger.warning("billing_act_provider_failed", code=exc.code)
        session.add(act)
        await session.flush()
        AuditRepository(session).record(
            AuditAction.BILLING_ACT_ISSUED,
            tenant_id=tenant.id,
            target_type="act",
            target_id=act.id,
            details={"month": month.isoformat(), "amount_kopecks": total},
        )
        await NotificationService(session).notify_admins(tenant.id, act_notice(act))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return 0
        return 1


async def admin_email(session: AsyncSession, tenant_id: object) -> str | None:
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


def requisites_notice(paid_until: date) -> Notice:
    last = paid_until - timedelta(days=1)
    return Notice(
        kind=NotificationKind.BILLING_INVOICE,
        title="Заполните реквизиты для счёта",
        lines=[
            f"Подписка оплачена по {last:%d.%m.%Y}. Чтобы выставить счёт на "
            "следующий период, нужны реквизиты компании.",
        ],
        link="/admin/company",
        action="Заполнить реквизиты",
    )


def overdue_notice(paid_until: date) -> Notice:
    last = paid_until - timedelta(days=1)
    return Notice(
        kind=NotificationKind.BILLING_OVERDUE,
        title="Подписка не оплачена",
        lines=[
            f"Подписка была оплачена по {last:%d.%m.%Y}, счёт на продление не "
            "оплачен. Оплатите его на странице тарифа или напишите нам, если "
            "нужна отсрочка.",
        ],
        link=TARIFF_PATH,
        action="Открыть тариф",
    )


def act_notice(act: Act) -> Notice:
    return Notice(
        kind=NotificationKind.BILLING_ACT,
        title=f"Акт за {month_name(act.month)}",
        lines=[
            f"Акт № {act.number} за {month_name(act.month)} на "
            f"{act.amount_kopecks // 100:,} ₽ — на странице тарифа, его можно "
            "скачать.".replace(",", " "),
        ],
        link=TARIFF_PATH,
        action="Открыть тариф",
    )
