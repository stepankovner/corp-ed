from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import IngestJob, IngestJobStatus


@dataclass(frozen=True)
class ClaimedJob:
    id: UUID
    tenant_id: UUID
    material_id: UUID
    attempts: int


# Одна задача атомарно: FOR UPDATE SKIP LOCKED — несколько воркеров не
# возьмут одну и ту же, и никто не ждёт чужой блокировки. Зависшая в
# RUNNING задача (воркер упал) возвращается в работу после stale_after.
_CLAIM = text(
    """
    UPDATE ingest_jobs
    SET status = 'RUNNING', locked_at = now(), attempts = attempts + 1,
        updated_at = now()
    WHERE id = (
        SELECT id FROM ingest_jobs
        WHERE (status = 'QUEUED' AND run_after <= now())
           OR (status = 'RUNNING'
               AND locked_at < now() - CAST(:stale_after AS interval))
        ORDER BY run_after
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, tenant_id, material_id, attempts
    """
)


class IngestJobRepository:
    """Очередь задач ингеста в Postgres.

    Таблица не под RLS (см. IngestJob): воркер выбирает задачу до того,
    как знает тенанта. Постановка в очередь идёт из контекста тенанта с
    tenant_id материала, а выполнение — уже в контексте тенанта задачи.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def enqueue(self, tenant_id: UUID, material_id: UUID) -> bool:
        """Поставить материал в очередь. False — активная задача уже есть.

        Коммит — у вызывающего: задача фиксируется вместе с тем, что её
        породило (создание материала, запрос переиндексации).
        """
        stmt = (
            insert(IngestJob)
            .values(
                tenant_id=tenant_id,
                material_id=material_id,
                status=IngestJobStatus.QUEUED,
            )
            .on_conflict_do_nothing(
                index_elements=["material_id"],
                index_where=text("status IN ('QUEUED', 'RUNNING')"),
            )
            .returning(IngestJob.id)
        )
        result = await self.session.execute(stmt)
        return result.first() is not None

    async def claim_next(self, *, stale_after: timedelta) -> ClaimedJob | None:
        row = (await self.session.execute(_CLAIM, {"stale_after": stale_after})).first()
        if row is None:
            return None
        return ClaimedJob(
            id=row.id,
            tenant_id=row.tenant_id,
            material_id=row.material_id,
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
            update(IngestJob).where(IngestJob.id == job_id).values(**values)
        )
