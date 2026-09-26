from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.db_policies import AUDIT_RETENTION_DAYS
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import AuditEvent
from corp_ed.repositories.connector_repository import SyncRunRepository
from corp_ed.repositories.qa_log_repository import QaLogRepository
from corp_ed.repositories.tenant_repository import TenantRepository

logger = structlog.get_logger()


@dataclass(frozen=True)
class PurgeReport:
    qa_log: int
    audit_events: int
    sync_runs: int = 0


class RetentionService:
    """Удаление данных по истечении срока хранения: python -m corp_ed.cli purge.

    Запускать по расписанию (cron / systemd timer раз в сутки). Журнал
    вопросов — по компании в своём tenant_scope (RLS); журнал аудита —
    одним запросом, триггер в базе пропустит только записи старше срока.
    """

    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        qa_log_days: int,
        sync_run_days: int = 90,
    ) -> None:
        self.session_maker = session_maker
        self.qa_log_days = qa_log_days
        self.sync_run_days = sync_run_days

    async def purge(self, now: datetime | None = None) -> PurgeReport:
        now = now or datetime.now(UTC)
        qa_cutoff = now - timedelta(days=self.qa_log_days)
        audit_cutoff = now - timedelta(days=AUDIT_RETENTION_DAYS)
        runs_cutoff = now - timedelta(days=self.sync_run_days)

        async with self.session_maker() as session:
            tenants = await TenantRepository(session).list_all()

        qa_deleted = 0
        runs_deleted = 0
        for tenant in tenants:
            with tenant_scope(tenant.id):
                async with self.session_maker() as session:
                    qa_deleted += await QaLogRepository(session).delete_older_than(
                        qa_cutoff
                    )
                    runs_deleted += await SyncRunRepository(session).delete_older_than(
                        runs_cutoff
                    )
                    await session.commit()

        async with self.session_maker() as session:
            result = await session.execute(
                delete(AuditEvent).where(AuditEvent.created_at < audit_cutoff)
            )
            await session.commit()
            audit_deleted = int(result.rowcount or 0)  # type: ignore[attr-defined]

        logger.info(
            "retention_purged",
            qa_log=qa_deleted,
            audit_events=audit_deleted,
            sync_runs=runs_deleted,
        )
        return PurgeReport(
            qa_log=qa_deleted, audit_events=audit_deleted, sync_runs=runs_deleted
        )
