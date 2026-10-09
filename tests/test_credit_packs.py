"""Купленные кредиты (решение владельца 09.10): после месячного пула —
пакеты, первыми те, что раньше сгорают; 402 — только когда пусто и там,
и там. Корректность при параллельных ответах — блокировка строки
компании на время списания."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.exceptions import CreditsExhaustedError
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.credit_packs import PACKS, add_months, pack_by_code, pack_expiry
from corp_ed.domain.models import (
    AuditEvent,
    CreditGrant,
    CreditOrder,
    CreditSpend,
    Notification,
    Tenant,
    User,
)
from tests.conftest import make_credit_service
from tests.team_notify_helpers import RecordingNotifier
from tests.test_credits import spend

NOW = datetime.now(UTC)


async def _pool(session: AsyncSession, tenant: Tenant, used: int) -> None:
    """Пул на одно место (420) и used потраченных кредитов в этом месяце."""
    tenant.seats = 1
    await session.commit()
    if used:
        user = await session.scalar(select(User).where(User.tenant_id == tenant.id))
        assert user is not None
        spend(session, user, used)
        await session.commit()


def grant(
    session: AsyncSession,
    tenant: Tenant,
    credits: int,
    *,
    expires_in: timedelta = timedelta(days=300),
    created_ago: timedelta = timedelta(days=1),
    remaining: int | None = None,
) -> CreditGrant:
    item = CreditGrant(
        tenant_id=tenant.id,
        credits=credits,
        remaining=credits if remaining is None else remaining,
        source="manual",
        created_at=datetime.now(UTC) - created_ago,
        expires_at=NOW + expires_in,
    )
    session.add(item)
    return item


async def _answer(session: AsyncSession, user: User, credits: int) -> None:
    """Ответ так, как его пишет FaqService: проверка, журнал, отметка."""
    service = make_credit_service(session)
    usage = await service.ensure_available(user)
    spend(session, user, credits)
    await session.flush()
    await service.note_spend(usage, credits)
    await session.commit()


async def _remaining(session: AsyncSession, item: CreditGrant) -> int:
    await session.refresh(item)
    return item.remaining


async def _spent(session: AsyncSession) -> int:
    return int(
        await session.scalar(select(func.coalesce(func.sum(CreditSpend.credits), 0)))
        or 0
    )


async def _events(session: AsyncSession, action: str) -> list[AuditEvent]:
    return list(
        (
            await session.scalars(select(AuditEvent).where(AuditEvent.action == action))
        ).all()
    )


# --- пакеты ----------------------------------------------------------------------


def test_packs_from_one_place() -> None:
    assert [(p.credits, p.price_kopecks) for p in PACKS] == [
        (500, 149_000),
        (2_000, 549_000),
        (5_000, 1_299_000),
    ]
    # Скидка за объём: кредит в большом пакете дешевле.
    prices = [p.price_kopecks / p.credits for p in PACKS]
    assert prices == sorted(prices, reverse=True)
    assert pack_by_code("pack_2000") == PACKS[1]
    assert pack_by_code("gold") is None


def test_packs_live_twelve_months() -> None:
    granted = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    assert pack_expiry(granted) == datetime(2027, 10, 9, 12, 0, tzinfo=UTC)
    assert add_months(datetime(2028, 2, 29, tzinfo=UTC), 12) == datetime(
        2029, 2, 28, tzinfo=UTC
    )
    assert add_months(datetime(2026, 11, 30, tzinfo=UTC), 3) == datetime(
        2027, 2, 28, tzinfo=UTC
    )


# --- списание: пул, потом пакеты ----------------------------------------------------


async def test_pool_first_then_packs(
    session: AsyncSession, tenant_ctx: Tenant, employee: User
) -> None:
    await _pool(session, tenant_ctx, 418)
    pack = grant(session, tenant_ctx, 500)
    await session.commit()

    await _answer(session, employee, 1)  # 419 — ещё пул
    assert await _remaining(session, pack) == 500
    await _answer(session, employee, 3)  # 422: 1 из пула, 2 из пакета
    assert await _remaining(session, pack) == 498
    await _answer(session, employee, 5)  # пул пуст — всё из пакета
    assert await _remaining(session, pack) == 493
    assert await _spent(session) == 7


async def test_earlier_expiring_pack_goes_first(
    session: AsyncSession, tenant_ctx: Tenant, employee: User
) -> None:
    await _pool(session, tenant_ctx, 420)
    # Купленный раньше, но сгорающий позже — вторым.
    later = grant(
        session,
        tenant_ctx,
        100,
        expires_in=timedelta(days=330),
        created_ago=timedelta(days=30),
    )
    sooner = grant(session, tenant_ctx, 100, expires_in=timedelta(days=60))
    await session.commit()

    await _answer(session, employee, 150)

    assert await _remaining(session, sooner) == 0
    assert await _remaining(session, later) == 50
    usage = await make_credit_service(session).usage()
    assert usage.purchased == 50
    assert usage.purchased_expires_at == later.expires_at


async def test_expired_pack_is_not_counted(
    session: AsyncSession, tenant_ctx: Tenant, employee: User
) -> None:
    await _pool(session, tenant_ctx, 420)
    old = grant(
        session,
        tenant_ctx,
        100,
        expires_in=-timedelta(days=1),
        created_ago=timedelta(days=366),
    )
    await session.commit()

    usage = await make_credit_service(session).usage()
    assert usage.purchased == 0
    with pytest.raises(CreditsExhaustedError):
        await make_credit_service(session).ensure_available(employee)
    assert await _remaining(session, old) == 100


async def test_last_pack_goes_into_small_minus(
    session: AsyncSession, tenant_ctx: Tenant, employee: User
) -> None:
    """Стоимость ответа заранее неизвестна: последний ответ может уйти в
    минус — его берёт грант, который сгорает последним."""
    await _pool(session, tenant_ctx, 420)
    first = grant(session, tenant_ctx, 10, expires_in=timedelta(days=30))
    last = grant(session, tenant_ctx, 10, expires_in=timedelta(days=300))
    await session.commit()

    await _answer(session, employee, 25)

    assert await _remaining(session, first) == 0
    assert await _remaining(session, last) == -5
    service = make_credit_service(session)
    assert (await service.usage()).purchased == 0
    with pytest.raises(CreditsExhaustedError):
        await service.ensure_available(employee)


async def test_minus_is_not_carried_to_a_new_pack(
    session: AsyncSession, tenant_ctx: Tenant, employee: User
) -> None:
    await _pool(session, tenant_ctx, 420)
    grant(session, tenant_ctx, 10, expires_in=timedelta(days=30), remaining=-3)
    fresh = grant(session, tenant_ctx, 100, expires_in=timedelta(days=360))
    await session.commit()

    assert (await make_credit_service(session).usage()).purchased == 100
    await _answer(session, employee, 2)
    assert await _remaining(session, fresh) == 98


async def test_pool_overrun_without_packs_is_not_charged_to_a_later_pack(
    session: AsyncSession, tenant_ctx: Tenant, employee: User
) -> None:
    """Пул ушёл в минус на границе, когда пакетов не было: купленный потом
    пакет этот минус не оплачивает."""
    await _pool(session, tenant_ctx, 419)
    await _answer(session, employee, 3)  # 422: минус 2, пакетов нет
    pack = grant(session, tenant_ctx, 100, created_ago=timedelta(0))
    await session.commit()

    await _answer(session, employee, 1)

    assert await _remaining(session, pack) == 99


# --- 402 -----------------------------------------------------------------------


async def test_stop_only_when_pool_and_packs_are_empty(
    session: AsyncSession, tenant_ctx: Tenant, employee: User
) -> None:
    service = make_credit_service(session)
    await _pool(session, tenant_ctx, 0)
    await service.ensure_available(employee)  # пул есть, пакетов нет

    await _pool(session, tenant_ctx, 420)
    with pytest.raises(CreditsExhaustedError):
        await service.ensure_available(employee)

    pack = grant(session, tenant_ctx, 5)
    await session.commit()
    usage = await service.ensure_available(employee)  # пул пуст, пакет есть
    assert (usage.exhausted, usage.stopped, usage.purchased) == (True, False, 5)

    await _answer(session, employee, 5)
    assert await _remaining(session, pack) == 0
    with pytest.raises(CreditsExhaustedError):
        await service.ensure_available(employee)


# --- параллельные ответы ------------------------------------------------------------


async def test_parallel_answers_on_the_boundary_are_charged_once(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    """Два ответа по 2 кредита, когда в пуле остался 1: перерасход — 3.

    Без блокировки каждый увидел бы только свой ответ и списал бы по 1.
    Второй ждёт, пока первый не зафиксируется, и видит его списание —
    ни дважды, ни мимо.
    """
    await _pool(session, tenant_ctx, 419)
    pack = grant(session, tenant_ctx, 100)
    await session.commit()

    async with session_maker() as first, session_maker() as second:
        one, two = make_credit_service(first), make_credit_service(second)
        before_one = await one.ensure_available(employee)
        before_two = await two.ensure_available(employee)

        spend(first, employee, 2)
        await first.flush()
        await one.note_spend(before_one, 2)

        spend(second, employee, 2)
        await second.flush()
        waiting = asyncio.create_task(two.note_spend(before_two, 2))
        # Первый ещё не зафиксирован — второй ждёт строку компании.
        done, _ = await asyncio.wait({waiting}, timeout=0.5)
        assert not done

        await first.commit()
        await waiting
        await second.commit()

    assert await _remaining(session, pack) == 97
    assert await _spent(session) == 3


# --- пороги и уведомления -----------------------------------------------------------


async def test_pool_exhausted_with_packs_left_warns_but_does_not_stop(
    session: AsyncSession, tenant_ctx: Tenant, employee: User, admin: User
) -> None:
    await _pool(session, tenant_ctx, 419)
    grant(session, tenant_ctx, 100)
    await session.commit()
    sent: list[str] = []
    service = make_credit_service(session)
    service.notifier = RecordingNotifier(sent)

    usage = await service.ensure_available(employee)
    spend(session, employee, 2)
    await session.flush()
    await service.note_spend(usage, 2)
    await session.commit()

    assert len(await _events(session, "credits.pool_exhausted")) == 1
    assert await _events(session, "credits.exhausted") == []
    # Команде пишем, только когда вопросы остановились.
    assert sent == []
    [notice] = (
        await session.scalars(
            select(Notification).where(
                Notification.user_id == admin.id,
                Notification.title == "Месячный пул кредитов израсходован",
            )
        )
    ).all()
    assert notice.kind == "credits_warning"
    assert "купленных кредитов: осталось 99" in notice.body
    assert "Купите пакет кредитов или добавьте места" in notice.body
    assert notice.link == "/admin/tariff"


async def test_exhausted_once_per_episode(
    session: AsyncSession, tenant_ctx: Tenant, employee: User, admin: User
) -> None:
    """Кредиты кончились — событие; докупили и снова кончились — ещё одно."""
    await _pool(session, tenant_ctx, 420)
    grant(session, tenant_ctx, 3, created_ago=timedelta(days=2))
    await session.commit()
    sent: list[str] = []

    service = make_credit_service(session)
    service.notifier = RecordingNotifier(sent)
    usage = await service.ensure_available(employee)
    spend(session, employee, 3)
    await session.flush()
    await service.note_spend(usage, 3)
    await session.commit()
    assert len(await _events(session, "credits.exhausted")) == 1
    assert len(sent) == 1

    grant(session, tenant_ctx, 2, created_ago=timedelta(0))
    await session.commit()
    usage = await service.ensure_available(employee)
    spend(session, employee, 2)
    await session.flush()
    await service.note_spend(usage, 2)
    await session.commit()

    assert len(await _events(session, "credits.exhausted")) == 2
    assert len(sent) == 2


# --- изоляция ------------------------------------------------------------------------


async def test_company_does_not_see_other_company_packs(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    admin: User,
) -> None:
    order_id = uuid4()
    session.add(
        CreditOrder(
            id=order_id,
            tenant_id=tenant_ctx.id,
            number=1,
            pack="pack_500",
            credits=500,
            amount_kopecks=149_000,
            created_by=admin.id,
        )
    )
    await session.flush()
    session.add(
        CreditGrant(
            tenant_id=tenant_ctx.id,
            credits=500,
            remaining=500,
            source="purchase",
            order_id=order_id,
            expires_at=NOW + timedelta(days=365),
        )
    )
    await session.commit()
    other = Tenant(id=uuid4(), company_code="other", name="Other")
    session.add(other)
    await session.commit()

    with tenant_scope(other.id):
        async with session_maker() as foreign:
            # Сырой SQL мимо ORM-хуков: отсекает политика RLS в базе.
            for table in ("credit_orders", "credit_grants"):
                count = await foreign.scalar(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
                assert count == 0, table
            usage = await make_credit_service(foreign).usage()
            assert usage.purchased == 0
