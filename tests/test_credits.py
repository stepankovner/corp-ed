"""Пул кредитов компании (досье 10.2): период, расход, пороги, остановка."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.config import EMBEDDING_DIM, BillingSettings
from corp_ed.core.exceptions import CreditsExhaustedError
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.credits import CreditUsage, billing_period, credits_for
from corp_ed.domain.models import AuditEvent, QaLog, Tenant, User
from corp_ed.llm.fake import FakeAdapter
from corp_ed.llm.fake_embedding import FakeEmbeddingAdapter
from corp_ed.services.faq_service import FaqService
from tests.conftest import make_credit_service

MOSCOW = ZoneInfo("Europe/Moscow")


def spend(
    session: AsyncSession,
    user: User,
    credits: int,
    *,
    created_at: datetime | None = None,
) -> None:
    """Запись журнала с расходом — как будто на вопрос уже ответили."""
    entry = QaLog(
        tenant_id=user.tenant_id,
        user_id=user.id,
        question="q",
        question_embedding=[0.1] * EMBEDDING_DIM,
        embedding_model="m",
        prompt_version="p",
        llm_model="m",
        answer_given=True,
        origin="documents",
        credits=credits,
    )
    if created_at is not None:
        entry.created_at = created_at
    session.add(entry)


async def _set_seats(session: AsyncSession, tenant: Tenant, seats: int) -> None:
    tenant.seats = seats
    await session.commit()


async def _events(session: AsyncSession, action: str) -> list[AuditEvent]:
    result = await session.execute(
        select(AuditEvent).where(AuditEvent.action == action)
    )
    return list(result.scalars())


# --- чистые функции -------------------------------------------------------------


@pytest.mark.parametrize(
    ("tokens", "credits"),
    [(0, 1), (1, 1), (2000, 1), (2001, 2), (10_000, 5)],
)
def test_credits_for(tokens: int, credits: int) -> None:
    assert credits_for(tokens, 2000) == credits


def test_period_is_moscow_calendar_month() -> None:
    # 1 октября 00:30 по Москве — это ещё 30 сентября по UTC.
    now = datetime(2026, 9, 30, 21, 30, tzinfo=UTC)

    start, end = billing_period(now, MOSCOW)

    assert start == datetime(2026, 10, 1, tzinfo=MOSCOW)
    assert start.astimezone(UTC) == datetime(2026, 9, 30, 21, 0, tzinfo=UTC)
    assert end == datetime(2026, 11, 1, tzinfo=MOSCOW)


def test_period_one_minute_before_moscow_midnight() -> None:
    now = datetime(2026, 9, 30, 20, 59, tzinfo=UTC)
    start, _ = billing_period(now, MOSCOW)
    assert start == datetime(2026, 9, 1, tzinfo=MOSCOW)


def test_period_rolls_over_year() -> None:
    start, end = billing_period(datetime(2026, 12, 15, tzinfo=UTC), MOSCOW)
    assert (start.month, end.year, end.month) == (12, 2027, 1)


def test_period_requires_aware_time() -> None:
    with pytest.raises(ValueError, match="aware"):
        billing_period(datetime(2026, 9, 1), MOSCOW)


def test_usage_arithmetic() -> None:
    usage = CreditUsage(
        period_start=datetime(2026, 9, 1, tzinfo=MOSCOW),
        period_end=datetime(2026, 10, 1, tzinfo=MOSCOW),
        seats=30,
        credits_per_seat=420,
        used=12_700,
    )
    assert usage.pool == 12_600
    assert usage.remaining == 0
    assert usage.exhausted is True


def test_unknown_timezone_fails_at_startup() -> None:
    with pytest.raises(ValidationError):
        BillingSettings(billing_timezone="Europe/Moskva")


@pytest.mark.parametrize(
    "field", ["credits_per_seat", "tokens_per_credit", "warn_at_percent"]
)
def test_billing_numbers_must_be_positive(field: str) -> None:
    with pytest.raises(ValidationError):
        BillingSettings(**{field: 0})


# --- расход -------------------------------------------------------------------


async def test_usage_counts_only_current_month(
    session: AsyncSession, tenant_ctx: Tenant, employee: User
) -> None:
    service = make_credit_service(session)
    start, _ = billing_period(datetime.now(UTC), MOSCOW)
    spend(session, employee, 5)
    spend(session, employee, 100, created_at=start - timedelta(minutes=1))
    await session.commit()

    usage = await service.usage()

    assert usage.used == 5
    assert usage.pool == 30 * 420
    assert usage.period_start == start


async def test_usage_does_not_count_other_company(
    session: AsyncSession, tenant_ctx: Tenant, employee: User
) -> None:
    other = Tenant(id=uuid4(), company_code="other", name="Other", seats=1)
    session.add(other)
    await session.commit()
    with tenant_scope(other.id):
        stranger = User(
            tenant_id=other.id, email="s@o.ru", role=employee.role, hashed_password="x"
        )
        session.add(stranger)
        await session.commit()
        spend(session, stranger, 1000)
        await session.commit()

    spend(session, employee, 3)
    await session.commit()

    assert (await make_credit_service(session).usage()).used == 3


async def test_seats_change_pool_immediately(
    session: AsyncSession, tenant_ctx: Tenant
) -> None:
    await _set_seats(session, tenant_ctx, 50)
    assert (await make_credit_service(session).usage()).pool == 50 * 420


# --- жёсткая остановка ------------------------------------------------------------


async def test_exhausted_pool_stops_before_paid_calls(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    faq_service: FaqService,
    fake_embeddings: FakeEmbeddingAdapter,
    fake_llm: FakeAdapter,
) -> None:
    await _set_seats(session, tenant_ctx, 1)
    spend(session, employee, 420)
    await session.commit()

    with pytest.raises(CreditsExhaustedError):
        await faq_service.answer("Сколько дней отпуска?", employee)

    # Ни эмбеддинга, ни модели: исчерпанный пул ничего не стоит.
    assert fake_embeddings.query_calls == []
    assert fake_llm.calls == []
    assert len((await session.execute(select(QaLog))).scalars().all()) == 1


async def test_last_credit_is_still_served(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    faq_service: FaqService,
) -> None:
    """Остановка — когда потрачено всё, а не «не хватит на следующий»:
    стоимость ответа заранее неизвестна."""
    await _set_seats(session, tenant_ctx, 1)
    spend(session, employee, 419)
    await session.commit()

    await faq_service.answer("Вопрос?", employee)

    with pytest.raises(CreditsExhaustedError):
        await faq_service.answer("Ещё вопрос?", employee)


async def test_new_month_restores_pool(
    session: AsyncSession, tenant_ctx: Tenant, employee: User
) -> None:
    await _set_seats(session, tenant_ctx, 1)
    start, _ = billing_period(datetime.now(UTC), MOSCOW)
    spend(session, employee, 420, created_at=start - timedelta(seconds=1))
    await session.commit()

    usage = await make_credit_service(session).ensure_available()

    assert usage.used == 0


# --- пороги ----------------------------------------------------------------------


async def test_warning_at_80_percent_is_recorded_once(
    session: AsyncSession, tenant_ctx: Tenant, employee: User
) -> None:
    await _set_seats(session, tenant_ctx, 1)  # пул 420, порог 336
    service = make_credit_service(session)
    spend(session, employee, 335)
    await session.commit()

    usage = await service.ensure_available()
    await service.note_spend(usage, 1)
    await session.commit()
    usage = await service.ensure_available()
    await service.note_spend(usage, 1)
    await session.commit()

    [event] = await _events(session, "credits.warning")
    assert event.tenant_id == tenant_ctx.id
    assert event.details["used"] == 336
    assert event.details["pool"] == 420
    assert await _events(session, "credits.exhausted") == []


async def test_below_threshold_records_nothing(
    session: AsyncSession, tenant_ctx: Tenant, employee: User
) -> None:
    await _set_seats(session, tenant_ctx, 1)
    service = make_credit_service(session)

    usage = await service.ensure_available()
    await service.note_spend(usage, 335)
    await session.commit()

    assert await _events(session, "credits.warning") == []


async def test_jump_over_both_thresholds_records_both(
    session: AsyncSession, tenant_ctx: Tenant, employee: User
) -> None:
    await _set_seats(session, tenant_ctx, 1)
    service = make_credit_service(session)
    spend(session, employee, 300)
    await session.commit()

    usage = await service.ensure_available()
    await service.note_spend(usage, 200)
    await session.commit()

    assert len(await _events(session, "credits.warning")) == 1
    assert len(await _events(session, "credits.exhausted")) == 1


async def test_missed_threshold_is_recorded_by_next_answer(
    session: AsyncSession, tenant_ctx: Tenant, employee: User
) -> None:
    """Два параллельных вопроса перешли порог, не видя друг друга:
    событие записывает следующий — порог проверяется как «достигнут»."""
    await _set_seats(session, tenant_ctx, 1)
    service = make_credit_service(session)
    spend(session, employee, 350)  # порог уже позади, события нет
    await session.commit()

    usage = await service.ensure_available()
    await service.note_spend(usage, 1)
    await session.commit()

    assert len(await _events(session, "credits.warning")) == 1


async def test_previous_month_event_does_not_suppress_new_one(
    session: AsyncSession, tenant_ctx: Tenant, employee: User
) -> None:
    await _set_seats(session, tenant_ctx, 1)
    service = make_credit_service(session)
    start, _ = billing_period(datetime.now(UTC), MOSCOW)
    session.add(
        AuditEvent(
            tenant_id=tenant_ctx.id,
            action="credits.warning",
            created_at=start - timedelta(days=3),
        )
    )
    spend(session, employee, 340)
    await session.commit()

    usage = await service.ensure_available()
    await service.note_spend(usage, 1)
    await session.commit()

    assert len(await _events(session, "credits.warning")) == 2


async def test_answer_records_threshold_in_same_transaction(
    session: AsyncSession,
    tenant_ctx: Tenant,
    employee: User,
    faq_service: FaqService,
) -> None:
    await _set_seats(session, tenant_ctx, 1)
    spend(session, employee, 419)
    await session.commit()

    await faq_service.answer("Вопрос?", employee)

    [event] = await _events(session, "credits.exhausted")
    assert event.details["used"] == 420
