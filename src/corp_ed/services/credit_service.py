import math
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import structlog

from corp_ed.core.exceptions import CreditsExhaustedError, NotFoundError
from corp_ed.core.tenant_context import require_tenant
from corp_ed.domain.credits import CreditUsage, billing_period, credits_for
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.repositories.qa_log_repository import QaLogRepository
from corp_ed.repositories.tenant_repository import TenantRepository

logger = structlog.get_logger()


def _utcnow() -> datetime:
    return datetime.now(UTC)


class CreditService:
    """Пул кредитов компании на месяц (досье 10.2).

    Решение команды 24.09: один пул на компанию, персональных лимитов
    нет, при исчерпании — жёсткая остановка до следующего месяца.
    Расход считается по qa_log: журнал ответов и есть книга расхода,
    отдельного счётчика, который мог бы с ним разойтись, нет.

    Сервис не коммитит: проверка и отметка порогов идут в транзакции
    ответа (FaqService), чтобы запись в журнал и событие о пороге
    фиксировались вместе.
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
    ) -> None:
        self.tenant_repo = tenant_repo
        self.qa_log_repo = qa_log_repo
        self.audit = audit
        self.credits_per_seat = credits_per_seat
        self.tokens_per_credit = tokens_per_credit
        self.zone = zone
        self.warn_at_percent = warn_at_percent
        self.now = now

    def cost(self, tokens: int) -> int:
        return credits_for(tokens, self.tokens_per_credit)

    async def usage(self) -> CreditUsage:
        """Расход текущей компании (из контекста) за текущий месяц."""
        tenant_id = require_tenant()
        tenant = await self.tenant_repo.get_by_id(tenant_id)
        if tenant is None:
            raise NotFoundError("Компания не найдена")
        start, end = billing_period(self.now(), self.zone)
        return CreditUsage(
            period_start=start,
            period_end=end,
            seats=tenant.seats,
            credits_per_seat=self.credits_per_seat,
            used=await self.qa_log_repo.credits_since(start),
        )

    async def ensure_available(self) -> CreditUsage:
        """Остановить обращение ДО платных вызовов, если пул исчерпан.

        Проверка без блокировки: параллельные вопросы на границе могут
        перерасходовать пул на несколько кредитов (см. DECISIONS.md).
        Сериализовать их блокировкой значило бы держать транзакцию на
        всё время ответа модели.
        """
        usage = await self.usage()
        if usage.exhausted:
            logger.warning(
                "credits_exhausted_rejected",
                tenant_id=str(require_tenant()),
                used=usage.used,
                pool=usage.pool,
            )
            raise CreditsExhaustedError()
        return usage

    async def note_spend(self, before: CreditUsage, spent: int) -> None:
        """Отметить пороги пула — один раз за месяц на каждый.

        Условие — «порог достигнут и события ещё нет», а не «порог
        перейдён этим вопросом»: два параллельных вопроса могут перейти
        порог вместе, не заметив друг друга, и тогда событие запишет
        следующий. Уведомление — событие в журнале аудита (его видит
        админ компании) и предупреждение в логе для команды.
        """
        used = before.used + spent
        warn_at = math.ceil(before.pool * self.warn_at_percent / 100)
        thresholds = (
            (AuditAction.CREDITS_WARNING, warn_at),
            (AuditAction.CREDITS_EXHAUSTED, before.pool),
        )
        tenant_id = require_tenant()
        for action, threshold in thresholds:
            if used < threshold:
                continue
            if await self.audit.exists_since(tenant_id, action, before.period_start):
                continue
            self.audit.record(
                action,
                tenant_id=tenant_id,
                target_type="tenant",
                target_id=tenant_id,
                details={
                    "used": used,
                    "pool": before.pool,
                    "period_start": before.period_start.isoformat(),
                },
            )
            logger.warning(
                "credits_threshold_reached",
                tenant_id=str(tenant_id),
                action=action.value,
                used=used,
                pool=before.pool,
            )
