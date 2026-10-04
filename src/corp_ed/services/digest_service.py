"""Недельная сводка администраторам (ТЗ §8).

По понедельникам с 9:00 по времени биллинга воркер рассылает сводку за
прошедшие 7 дней: вопросы, доля с ответом по документам, активные
сотрудники, частые вопросы, открытые пробелы. Раз в неделю на компанию:
отметка — событие журнала digest.sent с начала недели, поэтому перезапуск
воркера или второй воркер второй сводки не пришлют. Неделя без вопросов
— без сводки: пустое письмо каждую неделю учит его не открывать.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import Tenant
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.services.analytics_service import AnalyticsService, Overview
from corp_ed.services.notification_service import (
    Notice,
    NotificationKind,
    NotificationService,
)

logger = structlog.get_logger()

SEND_AT = time(9, 0)
FREQUENT_IN_DIGEST = 3


@dataclass(frozen=True)
class DigestReport:
    sent: int
    skipped: int


def week_start(now: datetime, zone: ZoneInfo) -> datetime:
    """Понедельник 00:00 текущей недели по времени биллинга."""
    local = now.astimezone(zone)
    monday = local.date() - timedelta(days=local.weekday())
    return datetime.combine(monday, time(0, 0), tzinfo=zone)


def digest_due(now: datetime, zone: ZoneInfo) -> bool:
    """Пора ли рассылать: с понедельника 9:00 до конца недели — если
    воркер лежал в понедельник, сводка придёт, когда он поднимется."""
    local = now.astimezone(zone)
    start = week_start(now, zone)
    return local >= datetime.combine(start.date(), SEND_AT, tzinfo=zone)


def plural(n: int, one: str, few: str, many: str) -> str:
    """1 вопрос, 2 вопроса, 5 вопросов."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def digest_notice(overview: Overview) -> Notice:
    share = (
        round(overview.answered / overview.questions * 100) if overview.questions else 0
    )
    lines = [
        f"За неделю — {overview.questions} "
        f"{plural(overview.questions, 'вопрос', 'вопроса', 'вопросов')}, "
        f"с ответом по документам {share} %.",
        f"Спрашивали {overview.active_people} из {overview.members} сотрудников.",
    ]
    if overview.frequent:
        top = "; ".join(
            f"«{item.question}»" for item in overview.frequent[:FREQUENT_IN_DIGEST]
        )
        lines.append(f"Чаще всего спрашивали: {top}.")
    if overview.open_gaps:
        lines.append(
            f"Открытых пробелов в документах: {overview.open_gaps} — вопросы, "
            "на которые в документах не нашлось ответа."
        )
    return Notice(
        kind=NotificationKind.WEEKLY_DIGEST,
        title=(
            f"Неделя в kronto: {overview.questions} "
            f"{plural(overview.questions, 'вопрос', 'вопроса', 'вопросов')}"
        ),
        lines=lines,
        link="/admin/overview",
        action="Открыть обзор",
    )


class DigestService:
    def __init__(
        self, session_maker: async_sessionmaker[AsyncSession], *, zone: str
    ) -> None:
        self.session_maker = session_maker
        self.zone = ZoneInfo(zone)

    async def send_due(
        self,
        now: datetime | None = None,
        *,
        force: bool = False,
        company_code: str | None = None,
    ) -> DigestReport:
        """Разослать сводки компаниям, которым они на этой неделе ещё не
        ушли. force — не ждать понедельника и не смотреть на отметку
        (cli digest --force: проверить письмо на стенде)."""
        moment = now or datetime.now(UTC)
        if not force and not digest_due(moment, self.zone):
            return DigestReport(sent=0, skipped=0)
        since = week_start(moment, self.zone)
        async with self.session_maker() as session:
            tenants = await TenantRepository(session).list_all()
        sent = skipped = 0
        for tenant in tenants:
            if not tenant.is_active:
                continue
            if company_code is not None and tenant.company_code != company_code:
                continue
            if await self._send(tenant, since, moment, force=force):
                sent += 1
            else:
                skipped += 1
        if sent:
            logger.info("digests_sent", sent=sent, skipped=skipped)
        return DigestReport(sent=sent, skipped=skipped)

    async def _send(
        self, tenant: Tenant, since: datetime, now: datetime, *, force: bool
    ) -> bool:
        with tenant_scope(tenant.id):
            async with self.session_maker() as session:
                audit = AuditRepository(session)
                if not force and await audit.exists_since(
                    tenant.id, AuditAction.DIGEST_SENT, since
                ):
                    return False
                overview = await AnalyticsService(session, zone=self.zone.key).overview(
                    7, now=now
                )
                # Отметка и при пустой неделе: иначе каждый час — новый подсчёт.
                audit.record(
                    AuditAction.DIGEST_SENT,
                    tenant_id=tenant.id,
                    target_type="tenant",
                    target_id=tenant.id,
                    details={"questions": overview.questions},
                )
                if overview.questions == 0:
                    await session.commit()
                    return False
                await NotificationService(session).notify_admins(
                    tenant.id, digest_notice(overview)
                )
                await session.commit()
        return True
