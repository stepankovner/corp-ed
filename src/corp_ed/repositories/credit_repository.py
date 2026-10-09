from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.tenant_context import require_tenant
from corp_ed.domain.models import (
    CreditGrant,
    CreditOrder,
    CreditSpend,
    CreditTopupRequest,
)


@dataclass(frozen=True)
class PurchasedBalance:
    """Купленные кредиты компании, которые ещё не сгорели."""

    remaining: int
    """Сумма положительных остатков действующих грантов. Минус последнего
    гранта не вычитается из нового пакета — так же, как минус пула не
    переносится на следующий месяц."""
    next_expiry: datetime | None
    """Когда сгорит ближайший грант с положительным остатком."""
    expiring: int
    """Сколько кредитов сгорит в next_expiry."""


class CreditRepository:
    """Купленные кредиты: заказы, гранты, списания, просьбы пополнить.

    Тенант-таблицы под RLS. Колоночные select и bulk UPDATE идут мимо
    ORM-хуков — фильтр по компании в них явный.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- гранты и списания -----------------------------------------------------

    async def active_grants(self, now: datetime) -> list[CreditGrant]:
        """Действующие гранты: первыми те, что раньше сгорают."""
        result = await self.session.scalars(
            select(CreditGrant)
            .where(
                CreditGrant.tenant_id == require_tenant(),
                CreditGrant.expires_at > now,
            )
            .order_by(CreditGrant.expires_at, CreditGrant.created_at, CreditGrant.id)
        )
        return list(result)

    async def balance(self, now: datetime) -> PurchasedBalance:
        grants = [g for g in await self.active_grants(now) if g.remaining > 0]
        if not grants:
            return PurchasedBalance(remaining=0, next_expiry=None, expiring=0)
        nearest = grants[0].expires_at
        return PurchasedBalance(
            remaining=sum(g.remaining for g in grants),
            next_expiry=nearest,
            expiring=sum(g.remaining for g in grants if g.expires_at == nearest),
        )

    async def spent_in_period(self, period_start: datetime) -> int:
        """Сколько кредитов месяца period_start уже списано сверх пула."""
        result = await self.session.scalar(
            select(func.coalesce(func.sum(CreditSpend.credits), 0)).where(
                CreditSpend.tenant_id == require_tenant(),
                CreditSpend.period_start == period_start,
            )
        )
        return int(result or 0)

    async def charge(self, amount: int, period_start: datetime, now: datetime) -> None:
        """Списать amount кредитов с действующих грантов, первыми — с тех,
        что раньше сгорают.

        Не хватило остатка — недостачу берёт грант, который сгорает
        последним, и уходит в небольшой минус: ответ уже дан, его
        стоимость заранее неизвестна. Грантов нет вовсе — строка без
        гранта: перерасход пула закрыт и не ляжет на пакет, купленный
        позже в том же месяце.

        Вызывать под блокировкой строки компании (CreditService): иначе два
        списания посчитали бы недостачу по одним и тем же данным.
        """
        tenant_id = require_tenant()
        grants = await self.active_grants(now)
        left = amount
        for grant in grants:
            if left == 0:
                break
            take = min(grant.remaining, left)
            if take <= 0:
                continue
            await self._take(grant, take, period_start)
            left -= take
        if left > 0:
            if grants:
                await self._take(grants[-1], left, period_start)
            else:
                self.session.add(
                    CreditSpend(
                        tenant_id=tenant_id,
                        grant_id=None,
                        credits=left,
                        period_start=period_start,
                    )
                )
        await self.session.flush()

    async def _take(
        self, grant: CreditGrant, credits: int, period_start: datetime
    ) -> None:
        # Атомарный UPDATE, а не «прочитал — записал»: остаток в объекте
        # мог устареть, база вычтет из актуального.
        await self.session.execute(
            update(CreditGrant)
            .where(CreditGrant.id == grant.id, CreditGrant.tenant_id == grant.tenant_id)
            .values(remaining=CreditGrant.remaining - credits)
            .execution_options(synchronize_session=False)
        )
        self.session.add(
            CreditSpend(
                tenant_id=grant.tenant_id,
                grant_id=grant.id,
                credits=credits,
                period_start=period_start,
            )
        )
        await self.session.refresh(grant, ["remaining"])

    async def latest_grant_at(self) -> datetime | None:
        """Когда кредиты зачислялись последний раз (с ним начинается новый
        эпизод исчерпания)."""
        latest: datetime | None = await self.session.scalar(
            select(func.max(CreditGrant.created_at)).where(
                CreditGrant.tenant_id == require_tenant()
            )
        )
        return latest

    def add_grant(self, grant: CreditGrant) -> CreditGrant:
        self.session.add(grant)
        return grant

    # --- заказы ----------------------------------------------------------------

    async def next_order_number(self) -> int:
        """Номер следующего заказа компании. Вызывать под блокировкой строки
        компании: два заказа не получат один номер (а получили бы — их
        остановит уникальность (tenant_id, number))."""
        result = await self.session.scalar(
            select(func.coalesce(func.max(CreditOrder.number), 0)).where(
                CreditOrder.tenant_id == require_tenant()
            )
        )
        return int(result or 0) + 1

    async def orders(self, limit: int) -> list[CreditOrder]:
        result = await self.session.scalars(
            select(CreditOrder)
            .order_by(CreditOrder.created_at.desc(), CreditOrder.number.desc())
            .limit(limit)
        )
        return list(result)

    async def order(
        self, order_id: UUID, *, for_update: bool = False
    ) -> CreditOrder | None:
        statement = select(CreditOrder).where(CreditOrder.id == order_id)
        if for_update:
            statement = statement.with_for_update()
        return (await self.session.scalars(statement)).first()

    async def grant_for_order(self, order_id: UUID) -> CreditGrant | None:
        return (
            await self.session.scalars(
                select(CreditGrant).where(CreditGrant.order_id == order_id)
            )
        ).first()

    # --- «Попросить администратора пополнить» -----------------------------------

    async def add_topup_request(self, episode_start: datetime, user_id: UUID) -> bool:
        """Отметить просьбу пополнить в этом эпизоде. False — уже просили:
        строку вставило другое нажатие (в том числе параллельное)."""
        result = await self.session.execute(
            insert(CreditTopupRequest)
            .values(
                tenant_id=require_tenant(),
                episode_start=episode_start,
                requested_by=user_id,
            )
            .on_conflict_do_nothing(constraint="uq_credit_topup_requests_episode")
            .returning(CreditTopupRequest.id)
        )
        return result.scalar_one_or_none() is not None

    async def topup_requested(self, episode_start: datetime) -> bool:
        result = await self.session.scalar(
            select(CreditTopupRequest.id).where(
                CreditTopupRequest.tenant_id == require_tenant(),
                CreditTopupRequest.episode_start == episode_start,
            )
        )
        return result is not None
