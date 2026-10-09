import calendar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.db_policies import AUDIT_RETENTION_DAYS
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import AuditEvent, AuthChallenge, TrustedDevice
from corp_ed.repositories.chat_repository import (
    AttachmentRepository,
    ConversationRepository,
)
from corp_ed.repositories.connector_repository import SyncRunRepository
from corp_ed.repositories.email_token_repository import EmailTokenRepository
from corp_ed.repositories.lead_repository import LeadRepository
from corp_ed.repositories.qa_log_repository import QaLogRepository
from corp_ed.repositories.refresh_token_repository import RefreshTokenRepository
from corp_ed.repositories.tenant_repository import TenantRepository

logger = structlog.get_logger()

REFRESH_TOKEN_GRACE = timedelta(days=30)
"""Сколько запись refresh-токена хранится после своего срока (expires_at).

Удаляются только истёкшие записи — по expires_at, не по used_at или
revoked_at. Это не ломает ни одну из проверок, которым записи нужны:

- закрытый сеанс (family_revoked): access-токен выдаётся вместе с
  refresh-записью (AuthService._issue), и она живёт дольше него — не
  меньше SESSION_REFRESH_TTL_HOURS (от часа) против
  ACCESS_TOKEN_TTL_MINUTES (до 60 минут). Пока действует хоть один
  access-токен сеанса, его refresh-запись не истекла и с отметкой
  revoked_at остаётся в таблице при любом запасе.
- повтор украденного токена (AuthService.refresh): used_at и revoked_at
  проверяются раньше срока, а used_at ставится только неистёкшей записи.
  Использованный токен отзывает цепочку весь свой срок
  (REFRESH_TOKEN_TTL_DAYS) и ещё REFRESH_TOKEN_GRACE после него. Позже
  украденный токен и так не действует — теряется лишь позднее
  обнаружение кражи и запись о нём в аудите.

Тридцать дней — с запасом на смену сроков в настройках и на
расследование. Первую запись живой цепочки репозиторий не удаляет: по
ней список сеансов показывает, когда начался вход."""

AUTH_RECORD_GRACE = timedelta(days=1)
"""Запас для шагов входа, ссылок из писем и доверенных устройств.

Их проверки одинаково отвечают на истёкшую, использованную и
отсутствующую запись, поэтому истёкшие удаляются почти сразу; сутки — на
расхождение часов и разбор жалобы «код не подошёл»."""


def months_before(moment: datetime, months: int) -> datetime:
    """moment минус months календарных месяцев, время суток то же.

    Если такого числа в том месяце нет, берётся последний день месяца:
    31 марта минус месяц — 28 февраля (29-е в високосный год), 31
    декабря минус полгода — 30 июня. Граница от этого только раньше, так
    что диалог никогда не удаляется до истечения полных N месяцев; зато
    в конце месяца у нескольких дней подряд граница одна (31.03, 30.03 и
    29.03 минус месяц — все 28.02), и диалог от 28.02 проживёт до 1.04 —
    на пару дней дольше, а не короче. Считается в UTC: по Москве граница
    сдвинута на три часа, для срока в месяцы это неважно.
    """
    total = moment.year * 12 + (moment.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


@dataclass(frozen=True)
class PurgeReport:
    qa_log: int
    audit_events: int
    sync_runs: int = 0
    leads: int = 0
    attachments: int = 0
    refresh_tokens: int = 0
    auth_challenges: int = 0
    email_tokens: int = 0
    trusted_devices: int = 0
    conversations: int = 0


class RetentionService:
    """Удаление данных по истечении срока хранения: python -m corp_ed.cli purge.

    Запускать по расписанию (cron / systemd timer раз в сутки). Журнал
    вопросов, запуски коннекторов, неотправленные вложения и диалоги чата
    — по компании в своём tenant_scope (RLS); у диалогов срок свой у
    каждой компании (chat_retention_months). Журнал аудита —
    одним запросом, триггер в базе пропустит только записи старше срока.
    Записи входа (refresh-токены, шаги входа, ссылки из писем, доверенные
    устройства) принадлежат учётке, а не компании, и вне RLS — тоже одним
    запросом на таблицу.
    """

    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        qa_log_days: int,
        sync_run_days: int = 90,
        lead_days: int = 180,
    ) -> None:
        self.session_maker = session_maker
        self.qa_log_days = qa_log_days
        self.sync_run_days = sync_run_days
        self.lead_days = lead_days

    async def purge(self, now: datetime | None = None) -> PurgeReport:
        now = now or datetime.now(UTC)
        qa_cutoff = now - timedelta(days=self.qa_log_days)
        audit_cutoff = now - timedelta(days=AUDIT_RETENTION_DAYS)
        runs_cutoff = now - timedelta(days=self.sync_run_days)
        leads_cutoff = now - timedelta(days=self.lead_days)
        # Вложения, не отправленные с вопросом (ТЗ §6), — через сутки.
        attachments_cutoff = now - timedelta(days=1)

        async with self.session_maker() as session:
            tenants = await TenantRepository(session).list_all()

        qa_deleted = 0
        runs_deleted = 0
        attachments_deleted = 0
        conversations_deleted = 0
        for tenant in tenants:
            # Диалоги — без активности дольше срока своей компании,
            # целиком: с сообщениями, вложениями и общей ссылкой.
            chat_cutoff = months_before(now, tenant.chat_retention_months)
            with tenant_scope(tenant.id):
                async with self.session_maker() as session:
                    qa_deleted += await QaLogRepository(session).delete_older_than(
                        qa_cutoff
                    )
                    runs_deleted += await SyncRunRepository(session).delete_older_than(
                        runs_cutoff
                    )
                    attachments_deleted += await AttachmentRepository(
                        session
                    ).delete_pending_older_than(attachments_cutoff)
                    conversations_deleted += await ConversationRepository(
                        session
                    ).delete_inactive_before(chat_cutoff)
                    await session.commit()

        async with self.session_maker() as session:
            result = await session.execute(
                delete(AuditEvent).where(AuditEvent.created_at < audit_cutoff)
            )
            await session.commit()
            audit_deleted = int(result.rowcount or 0)  # type: ignore[attr-defined]

        # Заявки на созвон — персональные данные тех, кто ещё не клиент.
        async with self.session_maker() as session:
            leads_deleted = await LeadRepository(session).delete_older_than(
                leads_cutoff
            )
            await session.commit()

        auth_cutoff = now - AUTH_RECORD_GRACE
        async with self.session_maker() as session:
            refresh_deleted = await RefreshTokenRepository(
                session
            ).delete_expired_before(now - REFRESH_TOKEN_GRACE)
            email_deleted = await EmailTokenRepository(session).delete_expired_before(
                auth_cutoff
            )
            challenges_deleted = await _delete_expired(
                session, AuthChallenge, auth_cutoff
            )
            devices_deleted = await _delete_expired(session, TrustedDevice, auth_cutoff)
            await session.commit()

        logger.info(
            "retention_purged",
            qa_log=qa_deleted,
            audit_events=audit_deleted,
            sync_runs=runs_deleted,
            leads=leads_deleted,
            attachments=attachments_deleted,
            refresh_tokens=refresh_deleted,
            auth_challenges=challenges_deleted,
            email_tokens=email_deleted,
            trusted_devices=devices_deleted,
            conversations=conversations_deleted,
        )
        return PurgeReport(
            qa_log=qa_deleted,
            audit_events=audit_deleted,
            sync_runs=runs_deleted,
            leads=leads_deleted,
            attachments=attachments_deleted,
            refresh_tokens=refresh_deleted,
            auth_challenges=challenges_deleted,
            email_tokens=email_deleted,
            trusted_devices=devices_deleted,
            conversations=conversations_deleted,
        )


async def _delete_expired(
    session: AsyncSession,
    model: type[AuthChallenge] | type[TrustedDevice],
    cutoff: datetime,
) -> int:
    result = await session.execute(delete(model).where(model.expires_at < cutoff))
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
