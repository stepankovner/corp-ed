"""Очередь синхронизации коннекторов: задачи, повторы, планировщик."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import Connector, ConnectorSyncJob, Tenant
from corp_ed.domain.types import ConnectorMode, SyncRunStatus, SyncTrigger
from corp_ed.repositories.connector_sync_job_repository import (
    ConnectorSyncJobRepository,
)
from corp_ed.services.connector_sync_service import SyncOutcome, SyncStats
from corp_ed.worker import SyncWorker


class StubSyncService:
    def __init__(self, outcomes: list[SyncOutcome | None | Exception]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[UUID, UUID, SyncTrigger]] = []

    async def run(
        self, tenant_id: UUID, connector_id: UUID, *, trigger: SyncTrigger
    ) -> SyncOutcome | None:
        self.calls.append((tenant_id, connector_id, trigger))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def failed(*, retryable: bool) -> SyncOutcome:
    return SyncOutcome(
        SyncRunStatus.FAILED, SyncStats(), "source_unavailable", retryable
    )


async def _connector(
    session: AsyncSession,
    tenant: Tenant,
    *,
    mode: ConnectorMode = ConnectorMode.ORGANIZATION,
    with_credentials: bool = True,
    status: str = "active",
    last_sync_at: datetime | None = None,
    interval: int = 60,
) -> Connector:
    with tenant_scope(tenant.id):
        connector = Connector(
            kind="fake_org",
            name="x",
            mode=mode.value,
            modules=["docs"],
            config={},
            credentials="cipher" if with_credentials else None,
            credentials_set_at=datetime.now(UTC) if with_credentials else None,
            status=status,
            last_sync_at=last_sync_at,
            sync_interval_minutes=interval,
        )
        session.add(connector)
        await session.commit()
    return connector


async def _jobs(session: AsyncSession) -> list[ConnectorSyncJob]:
    result = await session.execute(
        select(ConnectorSyncJob).execution_options(populate_existing=True)
    )
    return list(result.scalars())


def _worker(
    session_maker: async_sessionmaker[AsyncSession], service: StubSyncService
) -> SyncWorker:
    return SyncWorker(session_maker, service, max_attempts=2)  # type: ignore[arg-type]


async def test_empty_queue(session_maker: async_sessionmaker[AsyncSession]) -> None:
    assert not await _worker(session_maker, StubSyncService([])).run_once()


@pytest.mark.parametrize(
    ("outcome", "expected_status"),
    [
        (None, "DONE"),
        (SyncOutcome(SyncRunStatus.SUCCEEDED, SyncStats()), "DONE"),
        (SyncOutcome(SyncRunStatus.PARTIAL, SyncStats()), "DONE"),
        (failed(retryable=False), "FAILED"),
    ],
)
async def test_job_is_settled_by_outcome(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    outcome: SyncOutcome | None,
    expected_status: str,
) -> None:
    connector = await _connector(session, tenant_ctx)
    await ConnectorSyncJobRepository(session).enqueue(
        tenant_ctx.id, connector.id, SyncTrigger.MANUAL
    )
    await session.commit()
    service = StubSyncService([outcome])

    assert await _worker(session_maker, service).run_once()

    assert service.calls == [(tenant_ctx.id, connector.id, SyncTrigger.MANUAL)]
    [job] = await _jobs(session)
    assert job.status.name == expected_status


async def test_retryable_failure_requeues_then_gives_up(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
) -> None:
    connector = await _connector(session, tenant_ctx)
    await ConnectorSyncJobRepository(session).enqueue(
        tenant_ctx.id, connector.id, SyncTrigger.SCHEDULE
    )
    await session.commit()
    worker = _worker(session_maker, StubSyncService([failed(retryable=True)] * 2))

    await worker.run_once()
    [job] = await _jobs(session)
    assert job.status.name == "QUEUED" and job.attempts == 1
    assert job.run_after > datetime.now(UTC)
    assert job.last_error == "source_unavailable"

    job.run_after = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()
    await worker.run_once()
    [job] = await _jobs(session)
    assert job.status.name == "FAILED" and job.attempts == 2


async def test_crash_around_the_run_is_retried(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
) -> None:
    connector = await _connector(session, tenant_ctx)
    await ConnectorSyncJobRepository(session).enqueue(
        tenant_ctx.id, connector.id, SyncTrigger.SCHEDULE
    )
    await session.commit()
    await _worker(session_maker, StubSyncService([RuntimeError("db down")])).run_once()
    [job] = await _jobs(session)
    assert job.status.name == "QUEUED" and job.last_error == "internal_error"


async def test_enqueue_is_deduplicated(
    session: AsyncSession, tenant_ctx: Tenant
) -> None:
    connector = await _connector(session, tenant_ctx)
    jobs = ConnectorSyncJobRepository(session)
    assert await jobs.enqueue(tenant_ctx.id, connector.id, SyncTrigger.MANUAL)
    assert not await jobs.enqueue(tenant_ctx.id, connector.id, SyncTrigger.SCHEDULE)
    await session.commit()
    assert len(await _jobs(session)) == 1


async def test_scheduler_enqueues_only_due_connectors(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
) -> None:
    now = datetime.now(UTC)
    never_synced = await _connector(session, tenant_ctx)
    overdue = await _connector(
        session, tenant_ctx, last_sync_at=now - timedelta(hours=2)
    )
    recent = await _connector(
        session, tenant_ctx, last_sync_at=now - timedelta(minutes=5)
    )
    long_interval = await _connector(
        session, tenant_ctx, last_sync_at=now - timedelta(hours=2), interval=1440
    )
    paused = await _connector(session, tenant_ctx, status="paused")
    errored = await _connector(session, tenant_ctx, status="error")
    no_credentials = await _connector(session, tenant_ctx, with_credentials=False)
    per_user_without_credentials = await _connector(
        session, tenant_ctx, mode=ConnectorMode.PER_USER, with_credentials=False
    )
    suspended = Tenant(id=uuid4(), company_code="off", name="Off", is_active=False)
    session.add(suspended)
    await session.commit()
    in_suspended = await _connector(session, suspended)

    worker = _worker(session_maker, StubSyncService([]))
    assert await worker.schedule_due() == 3
    queued = {job.connector_id for job in await _jobs(session)}
    assert queued == {never_synced.id, overdue.id, per_user_without_credentials.id}
    for skipped in (
        recent,
        long_interval,
        paused,
        errored,
        no_credentials,
        in_suspended,
    ):
        assert skipped.id not in queued
    # Повторный тик не дублирует задачи.
    assert await worker.schedule_due() == 0
    assert all(job.trigger == "schedule" for job in await _jobs(session))
