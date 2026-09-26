from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import ConnectorSyncJob, IngestJobStatus
from corp_ed.domain.types import SyncTrigger


@dataclass(frozen=True)
class ClaimedSyncJob:
    id: UUID
    tenant_id: UUID
    connector_id: UUID
    trigger: SyncTrigger
    attempts: int


# Как в ingest_jobs: одна задача атомарно, зависшая RUNNING возвращается
# в работу после stale_after.
_CLAIM = text(
    """
    UPDATE connector_sync_jobs
    SET status = 'RUNNING', locked_at = now(), attempts = attempts + 1,
        updated_at = now()
    WHERE id = (
        SELECT id FROM connector_sync_jobs
        WHERE (status = 'QUEUED' AND run_after <= now())
           OR (status = 'RUNNING'
               AND locked_at < now() - CAST(:stale_after AS interval))
        ORDER BY run_after
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, tenant_id, connector_id, trigger, attempts
    """
)


class ConnectorSyncJobRepository:
    """Очередь синхронизации коннекторов (не под RLS, см. ConnectorSyncJob)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def enqueue(
        self, tenant_id: UUID, connector_id: UUID, trigger: SyncTrigger
    ) -> bool:
        """Поставить коннектор в очередь. False — активная задача уже есть."""
        stmt = (
            insert(ConnectorSyncJob)
            .values(
                tenant_id=tenant_id,
                connector_id=connector_id,
                trigger=trigger.value,
                status=IngestJobStatus.QUEUED,
            )
            .on_conflict_do_nothing(
                index_elements=["connector_id"],
                index_where=text("status IN ('QUEUED', 'RUNNING')"),
            )
            .returning(ConnectorSyncJob.id)
        )
        result = await self.session.execute(stmt)
        return result.first() is not None

    async def claim_next(self, *, stale_after: timedelta) -> ClaimedSyncJob | None:
        row = (await self.session.execute(_CLAIM, {"stale_after": stale_after})).first()
        if row is None:
            return None
        return ClaimedSyncJob(
            id=row.id,
            tenant_id=row.tenant_id,
            connector_id=row.connector_id,
            trigger=SyncTrigger(row.trigger),
            attempts=row.attempts,
        )

    async def mark_done(self, job_id: UUID) -> None:
        await self._set(job_id, status=IngestJobStatus.DONE, last_error=None)

    async def mark_failed(self, job_id: UUID, error: str) -> None:
        await self._set(job_id, status=IngestJobStatus.FAILED, last_error=error)

    async def retry_later(self, job_id: UUID, error: str, delay: timedelta) -> None:
        await self._set(
            job_id,
            status=IngestJobStatus.QUEUED,
            last_error=error,
            run_after=datetime.now(UTC) + delay,
            locked_at=None,
        )

    async def _set(self, job_id: UUID, **values: object) -> None:
        await self.session.execute(
            update(ConnectorSyncJob)
            .where(ConnectorSyncJob.id == job_id)
            .values(**values)
        )
