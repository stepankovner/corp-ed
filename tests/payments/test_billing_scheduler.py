"""Планировщик оплаты: счёт на продление за 5 дней, напоминание,
просрочка после льготного срока (только уведомления), сверка с банком
без вебхука, сокращение мест со следующего периода, автосписание по
карте, ежемесячный акт. Часы — подставные, банк — поддельная Точка."""

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.config import PaymentSettings, SellerSettings
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.billing import BillingPeriod, PaymentMethod
from corp_ed.domain.company_ref import company_ref
from corp_ed.domain.models import (
    Act,
    CompanyRequisites,
    Invoice,
    Notification,
    PaymentEvent,
    Subscription,
    Tenant,
    User,
)
from corp_ed.repositories.audit_repository import AuditRepository
from corp_ed.services.billing_scheduler import BillingScheduler
from corp_ed.services.billing_service import Billing, BillingService
from corp_ed.services.payment_service import PaymentService, StaffBillingService
from tests.payments.fake_tochka import FakeTochka
from tests.team_notify_helpers import RecordingNotifier

MSK = ZoneInfo("Europe/Moscow")


@dataclass
class Clock:
    moment: datetime

    def __call__(self) -> datetime:
        return self.moment

    def set(self, day: date, hour: int = 12) -> None:
        self.moment = datetime(day.year, day.month, day.day, hour, tzinfo=MSK)


@dataclass
class World:
    session: AsyncSession
    maker: async_sessionmaker[AsyncSession]
    tenant: Tenant
    admin: User
    bank: FakeTochka
    clock: Clock
    team: list[str]
    billing: Billing

    @property
    def payments(self) -> PaymentService:
        return PaymentService(self.maker, self.billing)

    @property
    def scheduler(self) -> BillingScheduler:
        return BillingScheduler(self.maker, self.billing, self.payments)

    @property
    def staff(self) -> StaffBillingService:
        return StaffBillingService(self.session, self.maker, self.billing)

    async def choose(self, period: BillingPeriod, method: PaymentMethod) -> Invoice:
        with tenant_scope(self.tenant.id):
            async with self.maker() as session:
                service = BillingService(
                    session, self.billing, AuditRepository(session)
                )
                invoice = await service.choose(self.admin, period, method)
        assert invoice is not None
        return invoice

    async def run(self) -> None:
        await self.scheduler.run_due()

    async def invoices(self) -> list[Invoice]:
        with tenant_scope(self.tenant.id):
            async with self.maker() as session:
                return list(
                    (
                        await session.scalars(select(Invoice).order_by(Invoice.number))
                    ).all()
                )

    async def subscription(self) -> Subscription:
        with tenant_scope(self.tenant.id):
            async with self.maker() as session:
                sub = await session.scalar(select(Subscription))
        assert sub is not None
        return sub

    async def notices(self, kind: str) -> list[Notification]:
        with tenant_scope(self.tenant.id):
            async with self.maker() as session:
                return list(
                    (
                        await session.scalars(
                            select(Notification).where(Notification.kind == kind)
                        )
                    ).all()
                )

    async def webhook(self, body: bytes) -> None:
        event = self.billing.require().parse_webhook(body)
        event_id = await self.payments.receive(event, body)
        assert event_id is not None
        await self.payments.process(event_id)


@pytest.fixture
async def world(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    admin: User,
) -> World:
    bank = FakeTochka()
    clock = Clock(datetime(2026, 10, 9, 12, tzinfo=MSK))
    team: list[str] = []
    billing = Billing(
        settings=PaymentSettings(provider="tochka"),
        provider=bank.provider(),
        seller=SellerSettings(name="ИП Тестов", inn="500100732259"),
        zone=MSK,
        site_url="https://kronto.test",
        notifier=RecordingNotifier(team),
        now=clock,
    )
    session.add(
        CompanyRequisites(
            tenant_id=tenant_ctx.id,
            legal_name="ООО «Ромашка»",
            inn="7707083893",
            kpp="773601001",
            payer_type="company",
            address="Москва",
            documents_email="buh@romashka.ru",
        )
    )
    await session.commit()
    return World(session, session_maker, tenant_ctx, admin, bank, clock, team, billing)


async def _pay_by_invoice(world: World, invoice: Invoice) -> None:
    assert invoice.provider_ref is not None
    world.bank.pay_bill(invoice.provider_ref)
    rubles = f"{invoice.amount_kopecks // 100}.{invoice.amount_kopecks % 100:02d}"
    await world.webhook(
        world.bank.incoming_webhook(
            amount=rubles, purpose=f"По счёту KR-{invoice.number:05d}", inn="7707083893"
        )
    )


async def _month_paid(world: World) -> Invoice:
    invoice = await world.choose(BillingPeriod.MONTH, PaymentMethod.INVOICE)
    await _pay_by_invoice(world, invoice)
    sub = await world.subscription()
    assert (sub.current_start, sub.current_end) == (
        date(2026, 10, 9),
        date(2026, 11, 9),
    )
    return invoice


# --- продление ------------------------------------------------------------------------


async def test_renewal_invoice_comes_five_days_ahead_once(world: World) -> None:
    await _month_paid(world)

    world.clock.set(date(2026, 11, 3))
    await world.run()
    assert len(await world.invoices()) == 1

    world.clock.set(date(2026, 11, 4))
    await world.run()
    await world.run()
    renewal = (await world.invoices())[-1]
    assert len(await world.invoices()) == 2
    assert (renewal.period_start, renewal.period_end, renewal.due_date) == (
        date(2026, 11, 9),
        date(2026, 12, 9),
        date(2026, 11, 9),
    )
    assert renewal.amount_kopecks == 30 * 990_00
    assert "09.11.2026–08.12.2026" in renewal.lines[0]["name"]
    assert len(await world.notices("billing_invoice")) == 1
    assert (await world.subscription()).status == "active"


async def test_overdue_notifies_the_team_once_and_blocks_nothing(world: World) -> None:
    await _month_paid(world)
    world.clock.set(date(2026, 11, 4))
    await world.run()

    world.clock.set(date(2026, 11, 9))
    await world.run()
    await world.run()
    assert (await world.subscription()).status == "awaiting_payment"
    # Напоминание в день окончания — одно.
    reminders = [
        n for n in await world.notices("billing_invoice") if "Напоминание" in n.title
    ]
    assert len(reminders) == 1
    assert world.team == []

    world.clock.set(date(2026, 11, 16))
    await world.run()
    await world.run()
    assert (await world.subscription()).status == "overdue"
    [message] = world.team
    # Компания — коротким id, не кодом (код повторяет название).
    assert company_ref(world.tenant.id) in message and "08.11.2026" in message
    assert world.tenant.company_code not in message
    assert len(await world.notices("billing_overdue")) == 1
    await world.session.refresh(world.tenant)
    assert world.tenant.is_active

    # Оплатили с опозданием — подписка снова активна.
    renewal = (await world.invoices())[-1]
    await _pay_by_invoice(world, renewal)
    sub = await world.subscription()
    assert (sub.status, sub.current_end) == ("active", date(2026, 12, 9))


async def test_reconciliation_finds_a_payment_without_webhook(world: World) -> None:
    await _month_paid(world)
    world.clock.set(date(2026, 11, 4))
    await world.run()
    renewal = (await world.invoices())[-1]
    assert renewal.provider_ref is not None
    world.bank.pay_bill(renewal.provider_ref)

    world.clock.set(date(2026, 11, 5))
    await world.run()

    sub = await world.subscription()
    assert sub.current_end == date(2026, 12, 9)
    events = (await world.session.scalars(select(PaymentEvent))).all()
    found = [e for e in events if e.problem == "found_by_reconciliation"]
    assert [(e.status, e.invoice_id) for e in found] == [("matched", renewal.id)]


async def test_fewer_seats_start_with_the_next_period(world: World) -> None:
    await _month_paid(world)
    await world.staff.defer_seats(world.tenant.id, 20)

    world.clock.set(date(2026, 11, 4))
    await world.run()
    renewal = (await world.invoices())[-1]
    assert (renewal.seats, renewal.amount_kopecks) == (20, 20 * 990_00)
    await _pay_by_invoice(world, renewal)
    await world.run()
    await world.session.refresh(world.tenant)
    assert world.tenant.seats == 30  # оплаченный период ещё идёт

    world.clock.set(date(2026, 11, 9))
    await world.run()
    await world.session.refresh(world.tenant)
    assert world.tenant.seats == 20
    assert (await world.subscription()).next_seats is None


async def test_deferred_seats_reissue_an_unpaid_renewal(world: World) -> None:
    await _month_paid(world)
    world.clock.set(date(2026, 11, 5))
    await world.run()
    first = (await world.invoices())[-1]

    await world.staff.defer_seats(world.tenant.id, 25)
    await world.run()

    invoices = await world.invoices()
    assert [(i.number, i.status, i.seats) for i in invoices[1:]] == [
        (first.number, "cancelled", 30),
        (first.number + 1, "awaiting_payment", 25),
    ]
    assert first.provider_ref in world.bank.deleted


# --- карта ----------------------------------------------------------------------------


async def test_card_renews_by_the_bank_schedule(world: World) -> None:
    link = await world.choose(BillingPeriod.MONTH, PaymentMethod.CARD)
    assert link.payment_url is not None and link.provider_ref is not None
    card = link.provider_ref
    assert world.bank.links[card]["kind"] == "subscriptions_with_receipt"
    world.bank.approve(card)
    await world.webhook(world.bank.acquiring_webhook(card))
    sub = await world.subscription()
    assert (sub.card_ref, sub.card_amount_kopecks) == (card, 30 * 990_00)

    world.clock.set(date(2026, 11, 4))
    await world.run()
    renewal = (await world.invoices())[-1]
    # Та же сумма — новой ссылки нет: банк спишет по графику.
    assert (renewal.provider_ref, renewal.payment_url) == (card, None)
    assert len(world.bank.links) == 1

    world.clock.set(date(2026, 11, 9))
    world.bank.approve(card)
    # Тело вебхука о втором списании такое же, как о первом.
    await world.webhook(world.bank.acquiring_webhook(card))
    sub = await world.subscription()
    assert sub.current_end == date(2026, 12, 9)
    assert [i.status for i in await world.invoices()] == ["paid", "paid"]


async def test_card_amount_change_needs_a_new_card_subscription(world: World) -> None:
    link = await world.choose(BillingPeriod.MONTH, PaymentMethod.CARD)
    assert link.provider_ref is not None
    world.bank.approve(link.provider_ref)
    await world.webhook(world.bank.acquiring_webhook(link.provider_ref))
    world.tenant.seats = 32
    await world.session.commit()

    world.clock.set(date(2026, 11, 4))
    await world.run()

    renewal = (await world.invoices())[-1]
    assert renewal.payment_url is not None
    assert renewal.amount_kopecks == 32 * 990_00
    assert world.bank.links[link.provider_ref]["status"] == "Cancelled"
    assert world.bank.links[renewal.provider_ref or ""]["Data"]["amount"] == 31680.0


# --- акты -----------------------------------------------------------------------------


async def test_monthly_act_for_paid_invoices(world: World) -> None:
    await _month_paid(world)

    world.clock.set(date(2026, 10, 31))
    await world.run()
    assert (await world.session.scalars(select(Act))).all() == []

    world.clock.set(date(2026, 11, 1), hour=9)
    await world.run()
    await world.run()
    with tenant_scope(world.tenant.id):
        [act] = (await world.session.scalars(select(Act))).all()
    assert (act.number, act.month, act.amount_kopecks) == (
        1,
        date(2026, 10, 1),
        30 * 990_00,
    )
    assert act.provider_ref in world.bank.acts
    bank_act = world.bank.acts[act.provider_ref or ""]["Data"]
    # Акт привязан к счёту банка и ушёл на почту для документов.
    assert bank_act["documentId"] == (await world.invoices())[0].provider_ref
    assert world.bank.emails == [(act.provider_ref, "buh@romashka.ru")]
    assert len(await world.notices("billing_act")) == 1

    with tenant_scope(world.tenant.id):
        async with world.maker() as session:
            service = BillingService(session, world.billing, AuditRepository(session))
            _, pdf = await service.act_pdf(act.id)
    assert pdf.startswith(b"%PDF")


async def test_no_act_without_payments(world: World) -> None:
    await world.choose(BillingPeriod.MONTH, PaymentMethod.INVOICE)
    world.clock.set(date(2026, 11, 2))
    await world.run()
    assert (await world.session.scalars(select(Act))).all() == []


async def test_scheduler_does_nothing_when_payments_are_off(world: World) -> None:
    await _month_paid(world)
    world.billing.settings = PaymentSettings(provider="none")
    world.clock.set(date(2026, 11, 20))
    report = await world.scheduler.run_due()
    assert (report.invoices, report.acts, report.overdue) == (0, 0, 0)
    assert len(await world.invoices()) == 1


async def test_new_choice_cancels_the_unpaid_card_link(world: World) -> None:
    first = await world.choose(BillingPeriod.MONTH, PaymentMethod.CARD)
    second = await world.choose(BillingPeriod.QUARTER, PaymentMethod.INVOICE)

    assert first.provider_ref is not None
    assert world.bank.links[first.provider_ref]["status"] == "Cancelled"
    assert [i.status for i in await world.invoices()] == [
        "cancelled",
        "awaiting_payment",
    ]
    assert second.amount_kopecks == 84_645_00
