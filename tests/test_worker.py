"""Фоновый ингест: очередь, повторы, изоляция тенантов, переиндексация."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.config import RagSettings
from corp_ed.core.exceptions import NotFoundError
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import (
    Chunk,
    IngestJob,
    IngestJobStatus,
    Material,
    MaterialStatus,
    Tenant,
)
from corp_ed.llm.embedding_gateway import EmbeddingGateway
from corp_ed.llm.errors import LLMError
from corp_ed.llm.fake_embedding import FakeEmbeddingAdapter
from corp_ed.llm.types import EmbeddingResult
from corp_ed.repositories.ingest_job_repository import IngestJobRepository
from corp_ed.services.ingest_service import IngestService
from corp_ed.services.reindex_service import ReindexService
from corp_ed.worker import (
    ERROR_NOT_FOUND,
    ERROR_PROVIDER,
    IngestWorker,
    retry_delay,
)

RAG = RagSettings(
    chunk_tokens=400,
    overlap_tokens=50,
    faq_limit=5,
    faq_max_distance=0.6,
    context_max_tokens=3000,
    faq_temperature=0.0,
)


class FailingEmbeddings(EmbeddingGateway):
    def __init__(self, *, retryable: bool) -> None:
        self.retryable = retryable

    async def embed_document(self, text: str) -> EmbeddingResult:
        raise LLMError("provider down", retryable=self.retryable)

    async def embed_query(self, text: str) -> EmbeddingResult:
        raise LLMError("provider down", retryable=self.retryable)


async def _material(
    session: AsyncSession, tenant: Tenant, title: str = "Док", content: str = "Текст."
) -> Material:
    with tenant_scope(tenant.id):
        material = Material(tenant_id=tenant.id, title=title, content=content)
        session.add(material)
        await session.flush()
        await IngestJobRepository(session).enqueue(tenant.id, material.id)
        await session.commit()
    return material


async def _jobs(session: AsyncSession) -> list[IngestJob]:
    result = await session.execute(
        select(IngestJob).execution_options(populate_existing=True)
    )
    return list(result.scalars())


async def _reload(session: AsyncSession, material: Material) -> Material:
    with tenant_scope(material.tenant_id):
        await session.refresh(material)
        await session.commit()
    return material


def _worker(
    maker: async_sessionmaker[AsyncSession],
    embeddings: EmbeddingGateway,
    max_attempts: int = 5,
) -> IngestWorker:
    return IngestWorker(maker, embeddings, RAG, max_attempts=max_attempts)


async def test_empty_queue(session_maker: async_sessionmaker[AsyncSession]) -> None:
    assert await _worker(session_maker, FakeEmbeddingAdapter()).run_once() is False


async def test_job_is_processed_in_its_tenant(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
) -> None:
    material = await _material(session, tenant_ctx)
    embeddings = FakeEmbeddingAdapter()

    assert await _worker(session_maker, embeddings).run_once() is True

    [job] = await _jobs(session)
    assert job.status is IngestJobStatus.DONE
    assert (await _reload(session, material)).status is MaterialStatus.READY
    assert len(embeddings.document_calls) == 1
    with tenant_scope(tenant_ctx.id):
        chunks = (await session.execute(select(Chunk))).scalars().all()
        assert chunks[0].tenant_id == tenant_ctx.id
        await session.commit()


async def test_worker_does_not_touch_other_tenants(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
) -> None:
    """Задача тенанта A пишет чанки только тенанту A."""
    other = Tenant(id=uuid4(), company_code="other", name="Other")
    session.add(other)
    await session.commit()
    mine = await _material(session, tenant_ctx, content="Моё.")
    theirs = await _material(session, other, content="Чужое.")

    worker = _worker(session_maker, FakeEmbeddingAdapter())
    assert await worker.run_once() and await worker.run_once()

    for tenant, material in ((tenant_ctx, mine), (other, theirs)):
        with tenant_scope(tenant.id):
            rows = (await session.execute(select(Chunk))).scalars().all()
            assert {chunk.material_id for chunk in rows} == {material.id}
            await session.commit()


async def test_retryable_error_requeues_with_backoff(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
) -> None:
    material = await _material(session, tenant_ctx)

    await _worker(session_maker, FailingEmbeddings(retryable=True)).run_once()

    [job] = await _jobs(session)
    assert job.status is IngestJobStatus.QUEUED
    assert job.attempts == 1
    assert job.run_after > datetime.now(UTC) + timedelta(seconds=20)
    reloaded = await _reload(session, material)
    assert reloaded.status is MaterialStatus.PENDING
    assert reloaded.status_error == ERROR_PROVIDER
    # Отложенная задача не берётся раньше времени.
    assert await _worker(session_maker, FakeEmbeddingAdapter()).run_once() is False


async def test_attempts_are_exhausted(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
) -> None:
    material = await _material(session, tenant_ctx)
    worker = _worker(session_maker, FailingEmbeddings(retryable=True), max_attempts=2)

    await worker.run_once()
    await session.execute(update(IngestJob).values(run_after=datetime.now(UTC)))
    await session.commit()
    await worker.run_once()

    [job] = await _jobs(session)
    assert job.status is IngestJobStatus.FAILED
    assert (await _reload(session, material)).status is MaterialStatus.FAILED


async def test_non_retryable_error_fails_at_once(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
) -> None:
    material = await _material(session, tenant_ctx)

    await _worker(session_maker, FailingEmbeddings(retryable=False)).run_once()

    [job] = await _jobs(session)
    assert job.status is IngestJobStatus.FAILED
    reloaded = await _reload(session, material)
    assert reloaded.status is MaterialStatus.FAILED
    # Клиенту — код, а не текст исключения провайдера.
    assert reloaded.status_error == ERROR_PROVIDER


async def test_failed_ingest_keeps_previous_chunks(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
) -> None:
    """Сбой посреди пересчёта не оставляет документ без чанков."""
    material = await _material(session, tenant_ctx)
    await _worker(session_maker, FakeEmbeddingAdapter()).run_once()
    with tenant_scope(tenant_ctx.id):
        await IngestJobRepository(session).enqueue(tenant_ctx.id, material.id)
        await session.commit()

    await _worker(session_maker, FailingEmbeddings(retryable=False)).run_once()

    with tenant_scope(tenant_ctx.id):
        chunks = (await session.execute(select(Chunk))).scalars().all()
        assert len(chunks) == 1
        await session.commit()


async def test_missing_material_fails_job(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Материал удалили между постановкой в очередь и запуском.

    Удаление материала каскадом убирает и задачу, поэтому гонка
    моделируется подменой: сервис сообщает, что материала уже нет.
    """
    await _material(session, tenant_ctx)

    async def gone(self: IngestService, material_id: object) -> int:
        raise NotFoundError("Материал с таким id не найден")

    monkeypatch.setattr(IngestService, "ingest", gone)

    assert await _worker(session_maker, FakeEmbeddingAdapter()).run_once() is True

    [job] = await _jobs(session)
    assert job.status is IngestJobStatus.FAILED
    assert job.last_error == ERROR_NOT_FOUND


async def test_two_workers_never_take_the_same_job(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
) -> None:
    """FOR UPDATE SKIP LOCKED: параллельные воркеры делят очередь."""
    for number in range(4):
        await _material(session, tenant_ctx, title=f"Док {number}")

    async def claim() -> object:
        async with session_maker() as own:
            job = await IngestJobRepository(own).claim_next(
                stale_after=timedelta(minutes=15)
            )
            await asyncio.sleep(0.05)
            await own.commit()
            return job.id if job else None

    claimed = await asyncio.gather(*(claim() for _ in range(4)))

    assert None not in claimed
    assert len(set(claimed)) == 4


async def test_stale_running_job_is_reclaimed(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
) -> None:
    """Воркер упал посреди задачи — через таймаут её берёт другой."""
    await _material(session, tenant_ctx)
    await session.execute(
        update(IngestJob).values(
            status=IngestJobStatus.RUNNING,
            locked_at=datetime.now(UTC) - timedelta(hours=1),
        )
    )
    await session.commit()

    assert await _worker(session_maker, FakeEmbeddingAdapter()).run_once() is True
    [job] = await _jobs(session)
    assert job.status is IngestJobStatus.DONE


async def test_enqueue_is_deduplicated(
    session: AsyncSession, tenant_ctx: Tenant
) -> None:
    material = await _material(session, tenant_ctx)
    with tenant_scope(tenant_ctx.id):
        again = await IngestJobRepository(session).enqueue(tenant_ctx.id, material.id)
        await session.commit()

    assert again is False
    assert len(await _jobs(session)) == 1


def test_retry_delay_grows() -> None:
    delays = [retry_delay(attempt) for attempt in range(1, 5)]
    assert delays == sorted(delays)
    assert delays[0] == timedelta(seconds=30)


# --- переиндексация (BH-6) ---------------------------------------------------


async def test_reindex_company_queues_every_material(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
) -> None:
    other = Tenant(id=uuid4(), company_code="other", name="Other")
    session.add(other)
    await session.commit()
    for tenant in (tenant_ctx, other):
        for number in range(2):
            await _material(session, tenant, title=f"Док {number}")
    await _drain(session_maker)

    reports = await ReindexService(session_maker).reindex(
        company_code="test", dry_run=False
    )

    assert [(r.company_code, r.materials, r.queued) for r in reports] == [
        ("test", 2, 2)
    ]
    active = [j for j in await _jobs(session) if j.status is IngestJobStatus.QUEUED]
    assert {j.tenant_id for j in active} == {tenant_ctx.id}


async def test_reindex_all_and_dry_run(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
) -> None:
    other = Tenant(id=uuid4(), company_code="other", name="Other")
    session.add(other)
    await session.commit()
    for tenant in (tenant_ctx, other):
        await _material(session, tenant)
    await _drain(session_maker)
    service = ReindexService(session_maker)

    dry = await service.reindex(company_code=None, dry_run=True)
    assert [(r.company_code, r.queued) for r in dry] == [("other", 0), ("test", 0)]

    real = await service.reindex(company_code=None, dry_run=False)
    assert [(r.company_code, r.queued) for r in real] == [("other", 1), ("test", 1)]


async def test_reindex_unknown_company(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(NotFoundError):
        await ReindexService(session_maker).reindex(company_code="nope", dry_run=False)


async def _drain(maker: async_sessionmaker[AsyncSession]) -> None:
    worker = _worker(maker, FakeEmbeddingAdapter())
    while await worker.run_once():
        pass
