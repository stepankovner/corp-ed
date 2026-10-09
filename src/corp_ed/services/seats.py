"""Места компании: сколько учёток можно держать активными и что будет с
пулом кредитов при смене числа мест (решения владельца продукта 28.09).

- П-2д: активных членств не больше оплаченных мест; заблокированные,
  ждущие одобрения и ушедшие не считаются. Место — сотрудник за
  компьютером (досье 5.2), цена — за место (10.4): сто человек на
  тридцати местах расходятся с моделью.
- П-2г: сокращение мест ниже уже потраченного за месяц останавливает
  вопросы до 1-го числа — `cli set-seats` предупреждает и просит --yes.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.config import get_billing_settings
from corp_ed.core.exceptions import SeatsLimitError
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.credits import billing_period
from corp_ed.domain.models import MemberStatus, Tenant, User
from corp_ed.repositories.credit_repository import CreditRepository
from corp_ed.repositories.qa_log_repository import QaLogRepository

ADMIN_SEATS_MESSAGE = (
    "Все места заняты: активных сотрудников {active} из {seats}. Уберите из "
    "компании тех, кто больше не работает, или напишите нам, чтобы добавить "
    "места."
)
JOIN_SEATS_MESSAGE = "В компании закончились места — сообщите администратору."


async def count_active_users(session: AsyncSession, tenant_id: UUID) -> int:
    result = await session.scalar(
        select(func.count())
        .select_from(User)
        .where(User.tenant_id == tenant_id, User.status == MemberStatus.ACTIVE)
    )
    return int(result or 0)


async def ensure_free_seat(
    session: AsyncSession, tenant_id: UUID, *, message: str = ADMIN_SEATS_MESSAGE
) -> None:
    """Отказать, если активных учёток уже столько, сколько мест.

    Строка компании блокируется до конца транзакции: два одновременных
    приглашения не займут одно последнее место.
    """
    tenant = (
        await session.scalars(
            select(Tenant).where(Tenant.id == tenant_id).with_for_update()
        )
    ).one()
    active = await count_active_users(session, tenant_id)
    if active >= tenant.seats:
        raise SeatsLimitError(message.format(active=active, seats=tenant.seats))


@dataclass(frozen=True)
class SeatsCheck:
    stops_pool: bool
    """Новый пул не больше потраченного: вопросы остановятся до 1-го."""
    message: str | None
    """Что сказать команде (или None, если всё спокойно)."""


async def seats_check(
    session: AsyncSession, company_code: str, seats: int
) -> SeatsCheck:
    """Последствия смены мест для `cli set-seats` — до изменения."""
    tenant = (
        await session.scalars(
            select(Tenant).where(Tenant.company_code == company_code.casefold())
        )
    ).first()
    if tenant is None:
        # Ошибку «компании нет» скажет сам set_seats.
        return SeatsCheck(stops_pool=False, message=None)
    billing = get_billing_settings()
    start, end = billing_period(_now(), billing.zone)
    with tenant_scope(tenant.id):
        used = await QaLogRepository(session).credits_since(start)
        active = await count_active_users(session, tenant.id)
        purchased = (await CreditRepository(session).balance(_now())).remaining
    pool = seats * billing.credits_per_seat
    notes: list[str] = []
    spent = used >= pool
    # Купленные кредиты (решение 09.10): пул кончился — вопросы идут из них.
    stops = spent and purchased <= 0
    if spent:
        then = (
            f"вопросы пойдут из купленных кредитов (осталось {purchased})."
            if purchased > 0
            else f"вопросы сотрудников остановятся до {end:%d.%m.%Y}."
        )
        notes.append(
            f"за месяц потрачено {used} кредитов, новый пул — {pool} "
            f"({seats} × {billing.credits_per_seat}): {then}"
        )
    if active > seats:
        notes.append(
            f"активных сотрудников {active} — больше мест: новых пустить нельзя, "
            "пока лишние не убраны из компании."
        )
    return SeatsCheck(stops_pool=stops, message=" ".join(notes) or None)


def _now() -> datetime:
    return datetime.now(UTC)
