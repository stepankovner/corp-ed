"""Пакеты кредитов: заказ, оплата по счёту, зачисление (решение владельца
09.10).

Поток: администратор выбирает пакет — заказ ждёт оплаты, команде уходит
уведомление в Telegram (без персональных данных); команда в нашей панели
отмечает «Оплачен» — кредиты зачисляются на 12 месяцев, администраторам
компании приходит «Кредиты зачислены». Неоплаченный заказ команда может
отменить. Ручное начисление (бонус, компенсация) — тоже из панели, с
комментарием в журнале.

Точки расширения следующего этапа:
- счёт (PDF) — InvoiceIssuer.issue при создании заказа, его id ложится в
  credit_orders.invoice_id;
- онлайн-оплата картой — payment_method='card', подтверждение платежа
  вызывает тот же StaffCreditService.mark_paid (без учётки команды).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.exceptions import CodedConflictError, NotFoundError
from corp_ed.core.tenant_context import require_tenant, tenant_scope
from corp_ed.domain.credit_packs import pack_by_code, pack_expiry
from corp_ed.domain.models import CreditGrant, CreditOrder, Tenant, User
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.repositories.credit_repository import CreditRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.services.notification_service import (
    Notice,
    NotificationKind,
    NotificationService,
)
from corp_ed.services.team_notify import (
    NULL_NOTIFIER,
    TeamNotifier,
    credit_order_message,
)

logger = structlog.get_logger()

ORDERS_LIMIT = 50
MAX_OPEN_ORDERS = 5
"""Неоплаченных заказов у компании не больше: защита от лишних кликов и
скрипта — каждый заказ приходит команде."""

AWAITING = "awaiting_payment"
PAID = "paid"
CANCELLED = "cancelled"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class InvoiceIssuer(Protocol):
    """Счёт на оплату заказа (PDF) — следующий этап. Вернёт id счёта для
    credit_orders.invoice_id; None — счёта нет."""

    async def issue(self, session: AsyncSession, order: CreditOrder) -> UUID | None: ...


class NoInvoices:
    """Счёт пока выставляет команда вручную."""

    async def issue(self, session: AsyncSession, order: CreditOrder) -> UUID | None:
        return None


NO_INVOICES = NoInvoices()


class CreditOrderService:
    """Заказы пакетов глазами администратора компании (из токена)."""

    def __init__(
        self,
        session: AsyncSession,
        audit: AuditRepository,
        *,
        notifier: TeamNotifier = NULL_NOTIFIER,
        invoices: InvoiceIssuer = NO_INVOICES,
    ) -> None:
        self.session = session
        self.audit = audit
        self.notifier = notifier
        self.invoices = invoices
        self.ledger = CreditRepository(session)

    async def orders(self) -> list[CreditOrder]:
        return await self.ledger.orders(ORDERS_LIMIT)

    async def create(self, admin: User, pack_code: str) -> CreditOrder:
        pack = pack_by_code(pack_code)
        if pack is None:
            raise CodedConflictError("Такого пакета кредитов нет", "unknown_pack")
        tenant_id = require_tenant()
        # Строка компании — на время выдачи номера: два заказа не получат
        # один номер.
        tenant = await TenantRepository(self.session).lock(tenant_id)
        open_orders = await self.session.scalar(
            select(func.count()).where(
                CreditOrder.tenant_id == tenant_id, CreditOrder.status == AWAITING
            )
        )
        if int(open_orders or 0) >= MAX_OPEN_ORDERS:
            raise CodedConflictError(
                "Уже есть несколько неоплаченных заказов — оплатите их или "
                "напишите в поддержку, чтобы отменить лишние",
                "too_many_orders",
            )
        order = CreditOrder(
            number=await self.ledger.next_order_number(),
            pack=pack.code,
            credits=pack.credits,
            amount_kopecks=pack.price_kopecks,
            payment_method="invoice",
            created_by=admin.id,
        )
        self.session.add(order)
        await self.session.flush()
        # Здесь подключится счёт (PDF) следующего этапа.
        order.invoice_id = await self.invoices.issue(self.session, order)
        self.audit.record(
            AuditAction.CREDITS_ORDER_CREATED,
            tenant_id=tenant_id,
            actor_id=admin.id,
            target_type="credit_order",
            target_id=order.id,
            details=_order_details(order),
        )
        await self.session.commit()
        logger.info(
            "credit_order_created",
            tenant_id=str(tenant_id),
            number=order.number,
            pack=pack.code,
        )
        self.notifier.notify(
            credit_order_message(
                company_code=tenant.company_code,
                number=order.number,
                credits=order.credits,
                amount_kopecks=order.amount_kopecks,
            )
        )
        return order


@dataclass(frozen=True)
class StaffOrder:
    order: CreditOrder
    tenant: Tenant


class StaffCreditService:
    """Наша панель: заказы всех компаний, «Оплачен», «Отменить», ручное
    начисление.

    Заказы и гранты — под RLS: у каждой компании своя сессия и свой
    tenant_scope (как StaffService). Действие, событие в журнале (с
    учёткой команды — current_staff) и уведомление администраторам
    фиксируются одной транзакцией.
    """

    def __init__(
        self,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
        *,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.session = session
        self.session_maker = session_maker
        self.now = now

    async def orders(self, status: str | None) -> list[StaffOrder]:
        tenants = (await self.session.scalars(select(Tenant))).all()
        result: list[StaffOrder] = []
        for tenant in tenants:
            with tenant_scope(tenant.id):
                async with self.session_maker() as session:
                    statement = select(CreditOrder).where(
                        CreditOrder.tenant_id == tenant.id
                    )
                    if status is not None:
                        statement = statement.where(CreditOrder.status == status)
                    orders = (await session.scalars(statement)).all()
            result += [StaffOrder(order=order, tenant=tenant) for order in orders]
        return sorted(result, key=lambda item: item.order.created_at, reverse=True)

    async def mark_paid(self, tenant_id: UUID, order_id: UUID) -> StaffOrder:
        """Оплата пришла: заказ оплачен, кредиты зачислены на 12 месяцев.

        Строка заказа блокируется: два нажатия «Оплачен» не зачислят дважды
        (второе увидит paid; уникальный credit_grants.order_id — второй
        рубеж).
        """
        tenant = await self._tenant(tenant_id)
        with tenant_scope(tenant.id):
            async with self.session_maker() as session:
                order = await self._open_order(session, order_id)
                now = self.now()
                order.status = PAID
                order.paid_at = now
                grant = CreditGrant(
                    credits=order.credits,
                    remaining=order.credits,
                    source="purchase",
                    order_id=order.id,
                    expires_at=pack_expiry(now),
                )
                session.add(grant)
                AuditRepository(session).record(
                    AuditAction.CREDITS_ORDER_PAID,
                    tenant_id=tenant.id,
                    target_type="credit_order",
                    target_id=order.id,
                    details={
                        **_order_details(order),
                        "expires_at": grant.expires_at.isoformat(),
                    },
                )
                await NotificationService(session).notify_admins(
                    tenant.id,
                    credits_added_notice(
                        order.credits, grant.expires_at, order_number=order.number
                    ),
                )
                await session.commit()
        logger.info("credit_order_paid", tenant_id=str(tenant.id), number=order.number)
        return StaffOrder(order=order, tenant=tenant)

    async def cancel(self, tenant_id: UUID, order_id: UUID) -> StaffOrder:
        tenant = await self._tenant(tenant_id)
        with tenant_scope(tenant.id):
            async with self.session_maker() as session:
                order = await self._open_order(session, order_id)
                order.status = CANCELLED
                order.cancelled_at = self.now()
                AuditRepository(session).record(
                    AuditAction.CREDITS_ORDER_CANCELLED,
                    tenant_id=tenant.id,
                    target_type="credit_order",
                    target_id=order.id,
                    details=_order_details(order),
                )
                await session.commit()
        return StaffOrder(order=order, tenant=tenant)

    async def grant(self, tenant_id: UUID, credits: int, comment: str) -> CreditGrant:
        """Начислить кредиты без заказа (бонус, компенсация). Комментарий —
        в журнал; администраторам — «Кредиты зачислены» без него: он для
        команды."""
        tenant = await self._tenant(tenant_id)
        with tenant_scope(tenant.id):
            async with self.session_maker() as session:
                grant = CreditGrant(
                    credits=credits,
                    remaining=credits,
                    source="manual",
                    comment=comment,
                    expires_at=pack_expiry(self.now()),
                )
                session.add(grant)
                await session.flush()
                AuditRepository(session).record(
                    AuditAction.CREDITS_GRANTED,
                    tenant_id=tenant.id,
                    target_type="credit_grant",
                    target_id=grant.id,
                    details={
                        "credits": credits,
                        "comment": comment,
                        "expires_at": grant.expires_at.isoformat(),
                    },
                )
                await NotificationService(session).notify_admins(
                    tenant.id, credits_added_notice(credits, grant.expires_at)
                )
                await session.commit()
        logger.info("credits_granted", tenant_id=str(tenant.id), credits=credits)
        return grant

    async def purchased(self, tenant_id: UUID) -> int:
        """Купленные кредиты компании, которые ещё не сгорели."""
        with tenant_scope(tenant_id):
            async with self.session_maker() as session:
                balance = await CreditRepository(session).balance(self.now())
        return balance.remaining

    async def _open_order(self, session: AsyncSession, order_id: UUID) -> CreditOrder:
        order = await CreditRepository(session).order(order_id, for_update=True)
        if order is None:
            raise NotFoundError("Заказ не найден")
        if order.status != AWAITING:
            raise CodedConflictError(
                "Заказ уже оплачен или отменён", "order_not_awaiting_payment"
            )
        return order

    async def _tenant(self, tenant_id: UUID) -> Tenant:
        tenant = await self.session.get(Tenant, tenant_id)
        if tenant is None:
            raise NotFoundError("Компания не найдена")
        return tenant


def _order_details(order: CreditOrder) -> dict[str, object]:
    return {
        "number": order.number,
        "pack": order.pack,
        "credits": order.credits,
        "amount_kopecks": order.amount_kopecks,
    }


def credits_added_notice(
    credits: int, expires_at: datetime, *, order_number: int | None = None
) -> Notice:
    source = f" по заказу № {order_number}" if order_number is not None else ""
    return Notice(
        kind=NotificationKind.CREDITS_ADDED,
        title="Кредиты зачислены",
        lines=[
            f"Компании зачислено {credits} кредитов{source}.",
            "Они расходуются после месячного пула и действуют до "
            f"{expires_at:%d.%m.%Y}.",
        ],
        link="/admin/tariff",
        action="Открыть тариф",
    )
