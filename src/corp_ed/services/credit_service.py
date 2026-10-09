from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import structlog

from corp_ed.core.exceptions import (
    CodedConflictError,
    CreditsExhaustedError,
    DailyLimitExhaustedError,
    NotFoundError,
)
from corp_ed.core.tenant_context import require_tenant
from corp_ed.domain.credits import (
    CreditUsage,
    billing_day,
    billing_period,
    credits_for,
)
from corp_ed.domain.models import Tenant, User
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.repositories.credit_repository import CreditRepository
from corp_ed.repositories.qa_log_repository import QaLogRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.services.notification_service import (
    Notice,
    NotificationKind,
    NotificationService,
)
from corp_ed.services.team_notify import (
    NULL_NOTIFIER,
    TeamNotifier,
    pool_exhausted_message,
)

logger = structlog.get_logger()

TARIFF_PATH = "/admin/tariff"
BUY_OR_ADD = "Купите пакет кредитов или добавьте места."


def _utcnow() -> datetime:
    return datetime.now(UTC)


class CreditService:
    """Кредиты компании: месячный пул (досье 10.2) и купленные пакеты.

    Решение команды 24.09: один пул на компанию, при исчерпании — жёсткая
    остановка. Решение владельца 09.10: остановленная компания может
    сразу докупить пакет; купленные кредиты живут 12 месяцев и
    списываются после пула, первыми — те, что раньше сгорают.

    Расход пула считается по qa_log: журнал ответов и есть книга расхода.
    Купленные кредиты — в своих таблицах (credit_grants, credit_spends):
    журнал живёт 90 дней, пакеты — год.

    Сервис не коммитит: проверка, списание с пакетов и отметка порогов
    идут в транзакции ответа (FaqService), чтобы запись в журнал,
    списание и событие о пороге фиксировались вместе.
    """

    def __init__(
        self,
        tenant_repo: TenantRepository,
        qa_log_repo: QaLogRepository,
        audit: AuditRepository,
        *,
        credits_per_seat: int,
        tokens_per_credit: int,
        zone: ZoneInfo,
        warn_at_percent: int,
        now: Callable[[], datetime] = _utcnow,
        notifier: TeamNotifier = NULL_NOTIFIER,
        notifications: NotificationService | None = None,
        ledger: CreditRepository | None = None,
    ) -> None:
        self.tenant_repo = tenant_repo
        self.qa_log_repo = qa_log_repo
        self.audit = audit
        self.credits_per_seat = credits_per_seat
        self.tokens_per_credit = tokens_per_credit
        self.zone = zone
        self.warn_at_percent = warn_at_percent
        self.now = now
        self.notifier = notifier
        # Колокольчик и письма администраторам — в той же сессии, что и
        # событие порога: нет события — нет и уведомления.
        self.notifications = notifications or NotificationService(audit.session)
        self.ledger = ledger or CreditRepository(audit.session)

    def cost(self, tokens: int) -> int:
        return credits_for(tokens, self.tokens_per_credit)

    async def usage(self) -> CreditUsage:
        """Расход текущей компании (из контекста) за текущий месяц и
        купленные кредиты, которые ещё не сгорели."""
        return await self._usage(await self._tenant())

    async def _tenant(self) -> Tenant:
        tenant = await self.tenant_repo.get_by_id(require_tenant())
        if tenant is None:
            raise NotFoundError("Компания не найдена")
        return tenant

    async def _usage(self, tenant: Tenant) -> CreditUsage:
        now = self.now()
        start, end = billing_period(now, self.zone)
        balance = await self.ledger.balance(now)
        return CreditUsage(
            period_start=start,
            period_end=end,
            seats=tenant.seats,
            credits_per_seat=self.credits_per_seat,
            used=await self.qa_log_repo.credits_since(start),
            warn_at_percent=self.warn_at_percent,
            purchased=balance.remaining,
            purchased_expires_at=balance.next_expiry,
            purchased_expiring=balance.expiring,
        )

    async def ensure_available(self, member: User | None = None) -> CreditUsage:
        """Остановить вопрос ДО платных вызовов, если кредитов нет: пул
        исчерпан и купленных не осталось, — или если человек израсходовал
        свой дневной лимит (настройка компании, по умолчанию выключен;
        действует на всех, включая администратора).

        Проверка без блокировки: параллельные вопросы на границе могут
        перерасходовать пул, пакет или дневной лимит на несколько кредитов
        (см. DECISIONS.md). Сериализовать их блокировкой значило бы держать
        транзакцию на всё время ответа модели.
        """
        tenant = await self._tenant()
        usage = await self._usage(tenant)
        if usage.stopped:
            logger.warning(
                "credits_exhausted_rejected",
                tenant_id=str(tenant.id),
                used=usage.used,
                pool=usage.pool,
            )
            raise CreditsExhaustedError()
        limit = tenant.daily_credits_per_member
        if member is not None and limit is not None:
            now = self.now()
            day_start, day_end = billing_day(now, self.zone)
            spent = await self.qa_log_repo.credits_since(day_start, user_id=member.id)
            if spent >= limit:
                logger.info(
                    "daily_limit_rejected",
                    tenant_id=str(tenant.id),
                    spent=spent,
                    limit=limit,
                )
                raise DailyLimitExhaustedError(
                    retry_after=int((day_end - now).total_seconds())
                )
        return usage

    # --- «Попросить администратора пополнить» ------------------------------------

    async def topup_status(self) -> tuple[bool, bool]:
        """Остановлены ли вопросы и просил ли уже кто-то пополнить в этом
        эпизоде исчерпания: тогда остальным — «Администратор уже уведомлён»."""
        usage = await self.usage()
        if not usage.stopped:
            return False, False
        return True, await self.ledger.topup_requested(await self.episode_start(usage))

    async def request_topup(self, member: User) -> bool:
        """Сотрудник упёрся в лимит и просит пополнить. Администраторам —
        одно уведомление на эпизод исчерпания, сколько бы людей ни нажали.

        Гонку двух нажатий решает уникальность (компания, начало эпизода):
        второе нажатие ждёт первое на уникальном индексе и ничего не
        вставляет. True — уведомление ушло сейчас; False — уже уведомлены.
        Коммитит.
        """
        usage = await self.usage()
        if not usage.stopped:
            raise CodedConflictError(
                "Кредиты есть — вопросы можно задавать", "credits_available"
            )
        tenant_id = require_tenant()
        episode = await self.episode_start(usage)
        if not await self.ledger.add_topup_request(episode, member.id):
            return False
        self.audit.record(
            AuditAction.CREDITS_TOPUP_REQUESTED,
            tenant_id=tenant_id,
            actor_id=member.id,
            target_type="tenant",
            target_id=tenant_id,
            details={"episode_start": episode.isoformat()},
        )
        until = usage.period_end.strftime("%d.%m.%Y")
        await self.notifications.notify_admins(
            tenant_id,
            Notice(
                kind=NotificationKind.CREDITS_TOPUP_REQUESTED,
                title="Сотрудники просят пополнить кредиты",
                lines=[
                    "Кредиты компании закончились — сотрудники не могут "
                    f"задавать вопросы до {until}.",
                    BUY_OR_ADD,
                ],
                link=TARIFF_PATH,
                action="Открыть тариф",
            ),
        )
        await self.audit.session.commit()
        return True

    async def note_spend(self, before: CreditUsage, spent: int) -> None:
        """Списать перерасход с пакетов и отметить пороги.

        Вызывать после того, как ответ записан в журнал (flush), в той же
        транзакции. Списание — под блокировкой строки компании (SELECT …
        FOR UPDATE), которая держится только от этой точки до коммита
        ответа, а не на время вызова модели (DECISIONS.md, 2026-09-25).
        Под блокировкой заново считаются расход месяца — с этим ответом и
        с ответами, уже зафиксированными другими транзакциями, — и то, что
        в этом месяце уже списано с пакетов. Разница — перерасход, который
        ещё никто не списал: параллельный ответ ждёт блокировку и потом
        видит и чужой ответ, и чужое списание, поэтому не спишет дважды и
        не пропустит.

        Пул вырос посреди месяца (добавили места) — разница отрицательная,
        уже списанное с пакетов не возвращается.

        Пороги — «достигнут и события ещё нет», а не «перейдён этим
        вопросом»: два параллельных вопроса могут перейти порог вместе, и
        тогда событие запишет следующий.
        """
        usage = await self._settle(before) if spent > 0 else before
        tenant_id = require_tenant()
        if usage.used >= usage.warn_at:
            await self._once(
                AuditAction.CREDITS_WARNING, usage, since=usage.period_start
            )
        if not usage.exhausted:
            return
        if not usage.stopped:
            # Пул кончился, вопросы идут из купленных кредитов. Если в этом
            # месяце кредиты уже кончались, админ знает про пул — молчим.
            if not await self.audit.exists_since(
                tenant_id, AuditAction.CREDITS_EXHAUSTED, usage.period_start
            ):
                await self._once(
                    AuditAction.CREDITS_POOL_EXHAUSTED, usage, since=usage.period_start
                )
            return
        if await self._once(
            AuditAction.CREDITS_EXHAUSTED, usage, since=await self.episode_start(usage)
        ):
            # Команде (П-5): компания остановилась — повод позвонить.
            tenant = await self.tenant_repo.get_by_id(tenant_id)
            self.notifier.notify(
                pool_exhausted_message(
                    company_code=tenant.company_code if tenant else str(tenant_id),
                    used=usage.used,
                    pool=usage.pool,
                    until=usage.period_end,
                )
            )

    async def _settle(self, before: CreditUsage) -> CreditUsage:
        """Под блокировкой строки компании: расход месяца и перерасход,
        который ещё не списан с пакетов (см. note_spend)."""
        tenant = await self.tenant_repo.lock(require_tenant())
        usage = replace(
            before,
            seats=tenant.seats,
            used=await self.qa_log_repo.credits_since(before.period_start),
        )
        overrun = max(0, usage.used - usage.pool)
        unpaid = overrun - await self.ledger.spent_in_period(before.period_start)
        now = self.now()
        if unpaid > 0:
            await self.ledger.charge(unpaid, before.period_start, now)
        balance = await self.ledger.balance(now)
        return replace(
            usage,
            purchased=balance.remaining,
            purchased_expires_at=balance.next_expiry,
            purchased_expiring=balance.expiring,
        )

    async def episode_start(self, usage: CreditUsage) -> datetime:
        """Начало эпизода исчерпания: начало месяца или последнее зачисление
        кредитов, что позже. Новый месяц или пополнение — новый эпизод."""
        latest = await self.ledger.latest_grant_at()
        if latest is None or latest < usage.period_start:
            return usage.period_start
        return latest

    async def _once(
        self, action: AuditAction, usage: CreditUsage, *, since: datetime
    ) -> bool:
        """Событие и уведомление администраторам, если с since их не было."""
        tenant_id = require_tenant()
        if await self.audit.exists_since(tenant_id, action, since):
            return False
        self.audit.record(
            action,
            tenant_id=tenant_id,
            target_type="tenant",
            target_id=tenant_id,
            details={
                "used": usage.used,
                "pool": usage.pool,
                "purchased": usage.purchased,
                "period_start": usage.period_start.isoformat(),
            },
        )
        logger.warning(
            "credits_threshold_reached",
            tenant_id=str(tenant_id),
            action=action.value,
            used=usage.used,
            pool=usage.pool,
            purchased=usage.purchased,
        )
        await self.notifications.notify_admins(tenant_id, credits_notice(action, usage))
        return True


def credits_notice(action: AuditAction, usage: CreditUsage) -> Notice:
    """Уведомление администраторам о пороге: что случилось и что делать —
    купить пакет или добавить места (страница «Тариф»)."""
    until = usage.period_end.strftime("%d.%m.%Y")
    spent = f"Израсходовано {usage.used} из {usage.pool} кредитов месячного пула."
    if action is AuditAction.CREDITS_EXHAUSTED:
        return Notice(
            kind=NotificationKind.CREDITS_EXHAUSTED,
            title="Кредиты закончились",
            lines=[
                f"{spent} Купленных кредитов не осталось.",
                f"Сотрудники не смогут задавать вопросы до {until}. {BUY_OR_ADD}",
            ],
            link=TARIFF_PATH,
            action="Открыть тариф",
        )
    if action is AuditAction.CREDITS_POOL_EXHAUSTED:
        return Notice(
            kind=NotificationKind.CREDITS_WARNING,
            title="Месячный пул кредитов израсходован",
            lines=[
                spent,
                "Вопросы списываются с купленных кредитов: осталось "
                f"{usage.purchased}. Когда они кончатся, вопросы остановятся "
                f"до {until}. {BUY_OR_ADD}",
            ],
            link=TARIFF_PATH,
            action="Открыть тариф",
        )
    reserve = (
        f" В запасе {usage.purchased} купленных кредитов."
        if usage.purchased > 0
        else ""
    )
    return Notice(
        kind=NotificationKind.CREDITS_WARNING,
        title=f"Кредиты: израсходовано {usage.warn_at_percent} % пула",
        lines=[
            spent + reserve,
            f"Когда кредиты закончатся, вопросы остановятся до {until}. {BUY_OR_ADD}",
        ],
        link=TARIFF_PATH,
        action="Открыть тариф",
    )
