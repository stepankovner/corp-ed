"""Воркер фоновых задач: python -m corp_ed.worker

Два цикла в одном процессе:
- IngestWorker — задачи ingest_jobs (нарезка, эмбеддинги, замена чанков);
- SyncWorker — задачи connector_sync_jobs (синхронизация коннекторов) и
  планировщик, который раз в минуту ставит в очередь подключения с
  истёкшим интервалом.

Каждая задача выполняется в контексте своего тенанта, временные сбои
повторяются с растущей паузой. Останавливается по SIGTERM/SIGINT после
текущей задачи — docker stop не рвёт её посередине.

Правило изоляции: одна задача — одна свежая сессия и один tenant_scope.
Identity map сессии, пережившей задачу другого тенанта, отдал бы его
объекты мимо фильтра (DECISIONS.md, «session.get обходит фильтр»).
"""

import asyncio
import contextlib
import signal
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.registry import default_registry
from corp_ed.core.config import (
    LLMSettings,
    RagSettings,
    get_connector_settings,
    get_http_settings,
)
from corp_ed.core.database import get_session_maker
from corp_ed.core.exceptions import NotFoundError
from corp_ed.core.logging import configure_logging
from corp_ed.core.outbound import OutboundClient
from corp_ed.core.secrets import SecretBox
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import MaterialStatus
from corp_ed.domain.types import SyncRunStatus, SyncTrigger
from corp_ed.llm.embedding_gateway import EmbeddingGateway
from corp_ed.llm.errors import LLMError
from corp_ed.llm.throttle import InMemoryThrottle, RedisThrottle, Throttle
from corp_ed.llm.yandex_embedding import YandexEmbeddingAdapter
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.repositories.connector_repository import ConnectorRepository
from corp_ed.repositories.connector_sync_job_repository import (
    ClaimedSyncJob,
    ConnectorSyncJobRepository,
)
from corp_ed.repositories.ingest_job_repository import (
    ClaimedJob,
    IngestJobRepository,
)
from corp_ed.repositories.material_repository import MaterialRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.services.connector_sync_service import ConnectorSyncService
from corp_ed.services.ingest_service import IngestService

logger = structlog.get_logger()

MAX_ATTEMPTS = 5
STALE_AFTER = timedelta(minutes=15)
"""Задача в RUNNING дольше этого — воркер упал; её берёт другой."""
IDLE_SLEEP = 2.0
INGEST_MAX_WAIT = 300.0
"""Сколько задача готова ждать слота квоты эмбеддингов."""

# Коды ошибок для материала: видны админу компании. Текст исключения и
# ответ провайдера — только в логе.
ERROR_PROVIDER = "embedding_provider_error"
ERROR_NOT_FOUND = "material_not_found"
ERROR_INTERNAL = "internal_error"


def retry_delay(attempt: int) -> timedelta:
    """30 с, 1 мин, 2 мин, 4 мин… — сбой провайдера обычно проходит сам."""
    return timedelta(seconds=30 * 2 ** (attempt - 1))


class IngestWorker:
    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        embedding_gateway: EmbeddingGateway,
        rag: RagSettings,
        *,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self.session_maker = session_maker
        self.embedding_gateway = embedding_gateway
        self.rag = rag
        self.max_attempts = max_attempts

    async def run_once(self) -> bool:
        """Выполнить одну задачу. False — очередь пуста."""
        async with self.session_maker() as session:
            job = await IngestJobRepository(session).claim_next(stale_after=STALE_AFTER)
            await session.commit()
        if job is None:
            return False

        log = logger.bind(
            job_id=str(job.id),
            tenant_id=str(job.tenant_id),
            material_id=str(job.material_id),
            attempt=job.attempts,
        )
        with tenant_scope(job.tenant_id):
            async with self.session_maker() as session:
                await self._process(session, job, log)
        return True

    async def run_forever(self, stop: asyncio.Event) -> None:
        logger.info("worker_started")
        while not stop.is_set():
            try:
                worked = await self.run_once()
            except Exception:
                # Сбой самой очереди (база недоступна): не падаем, ждём.
                logger.exception("worker_loop_error")
                worked = False
            if not worked:
                # Пустая очередь: пауза, но с немедленным выходом по сигналу.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=IDLE_SLEEP)
        logger.info("worker_stopped")

    async def _process(
        self, session: AsyncSession, job: ClaimedJob, log: structlog.BoundLogger
    ) -> None:
        jobs = IngestJobRepository(session)
        service = IngestService(
            material_repo=MaterialRepository(session),
            chunk_repo=ChunkRepository(session),
            embedding_gateway=self.embedding_gateway,
            session=session,
            chunk_tokens=self.rag.chunk_tokens,
            overlap_tokens=self.rag.overlap_tokens,
        )
        try:
            chunks = await service.ingest(job.material_id)
        except NotFoundError:
            # Материал удалили, пока задача ждала: делать нечего.
            await session.rollback()
            await jobs.mark_failed(job.id, ERROR_NOT_FOUND)
            await session.commit()
            log.info("ingest_skipped_missing_material")
            return
        except LLMError as exc:
            await self._fail(session, service, job, ERROR_PROVIDER, exc.retryable)
            log.warning("ingest_provider_error", error=str(exc))
            return
        except Exception:
            await self._fail(session, service, job, ERROR_INTERNAL, retryable=False)
            log.exception("ingest_failed")
            return

        await jobs.mark_done(job.id)
        await session.commit()
        log.info("ingest_done", chunks=chunks)

    async def _fail(
        self,
        session: AsyncSession,
        service: IngestService,
        job: ClaimedJob,
        error: str,
        retryable: bool,
    ) -> None:
        jobs = IngestJobRepository(session)
        if retryable and job.attempts < self.max_attempts:
            await service.mark(job.material_id, MaterialStatus.PENDING, error)
            await jobs.retry_later(job.id, error, retry_delay(job.attempts))
        else:
            await service.mark(job.material_id, MaterialStatus.FAILED, error)
            await jobs.mark_failed(job.id, error)
        await session.commit()


SYNC_MAX_ATTEMPTS = 3
SYNC_STALE_AFTER = timedelta(hours=3)
"""Запуск синхронизации длится до CONNECTOR_MAX_RUN_MINUTES; дольше
трёх часов в RUNNING — воркер упал."""
SCHEDULE_EVERY = 60.0
SYNC_ERROR_INTERNAL = "internal_error"


class SyncWorker:
    """Очередь синхронизации коннекторов и её планировщик."""

    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        service: ConnectorSyncService,
        *,
        max_attempts: int = SYNC_MAX_ATTEMPTS,
        schedule_every: float = SCHEDULE_EVERY,
    ) -> None:
        self.session_maker = session_maker
        self.service = service
        self.max_attempts = max_attempts
        self.schedule_every = schedule_every

    async def run_once(self) -> bool:
        """Выполнить одну задачу. False — очередь пуста."""
        async with self.session_maker() as session:
            job = await ConnectorSyncJobRepository(session).claim_next(
                stale_after=SYNC_STALE_AFTER
            )
            await session.commit()
        if job is None:
            return False
        log = logger.bind(
            job_id=str(job.id),
            tenant_id=str(job.tenant_id),
            connector_id=str(job.connector_id),
            attempt=job.attempts,
        )
        try:
            outcome = await self.service.run(
                job.tenant_id, job.connector_id, trigger=job.trigger
            )
        except Exception:
            # Сам сервис ловит всё и пишет в журнал запуска; сюда доходит
            # только сбой до или после запуска (база недоступна).
            log.exception("sync_job_crashed")
            await self._settle(job, retryable=True, error=SYNC_ERROR_INTERNAL)
            return True
        if outcome is None or outcome.status is not SyncRunStatus.FAILED:
            await self._settle(job, retryable=False, error=None)
        else:
            await self._settle(
                job, retryable=outcome.retryable, error=outcome.error_code
            )
        return True

    async def _settle(
        self, job: ClaimedSyncJob, *, retryable: bool, error: str | None
    ) -> None:
        async with self.session_maker() as session:
            jobs = ConnectorSyncJobRepository(session)
            if error is None:
                await jobs.mark_done(job.id)
            elif retryable and job.attempts < self.max_attempts:
                await jobs.retry_later(job.id, error, retry_delay(job.attempts))
            else:
                await jobs.mark_failed(job.id, error)
            await session.commit()

    async def schedule_due(self) -> int:
        """Поставить в очередь подключения, чей интервал истёк.

        По компаниям в своём tenant_scope: таблица connectors под RLS, а
        планировщик — единственное место, где нужны все компании сразу.
        """
        now = datetime.now(UTC)
        async with self.session_maker() as session:
            tenants = await TenantRepository(session).list_all()
        queued = 0
        for tenant in tenants:
            if not tenant.is_active:
                continue
            with tenant_scope(tenant.id):
                async with self.session_maker() as session:
                    due = await ConnectorRepository(session).list_due(now)
                    jobs = ConnectorSyncJobRepository(session)
                    for connector in due:
                        if await jobs.enqueue(
                            tenant.id, connector.id, SyncTrigger.SCHEDULE
                        ):
                            queued += 1
                    await session.commit()
        if queued:
            logger.info("sync_scheduled", queued=queued)
        return queued

    async def run_forever(self, stop: asyncio.Event) -> None:
        logger.info("sync_worker_started")
        next_schedule = 0.0
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            try:
                if loop.time() >= next_schedule:
                    await self.schedule_due()
                    next_schedule = loop.time() + self.schedule_every
                worked = await self.run_once()
            except Exception:
                logger.exception("sync_worker_loop_error")
                worked = False
            if not worked:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=IDLE_SLEEP)
        logger.info("sync_worker_stopped")


def _ingest_throttle(redis: Redis | None, rate: float) -> Throttle:
    if redis is None:
        return InMemoryThrottle(rate, max_wait=INGEST_MAX_WAIT)
    return RedisThrottle(redis, "embedding-ingest", rate, max_wait=INGEST_MAX_WAIT)


async def main(install_signals: Callable[[asyncio.Event], None] | None = None) -> None:
    configure_logging()
    llm = LLMSettings()  # type: ignore[call-arg]
    rag = RagSettings()  # type: ignore[call-arg]
    http = get_http_settings()

    redis = (
        Redis.from_url(http.redis_url.get_secret_value()) if http.redis_url else None
    )
    stop = asyncio.Event()
    (install_signals or _install_signals)(stop)

    async with httpx.AsyncClient() as client:
        gateway = YandexEmbeddingAdapter(
            client=client,
            folder_id=llm.yc_folder_id,
            api_key=llm.yc_api_key.get_secret_value(),
            family=llm.embedding_model,
            dim=llm.embedding_dim,
            document_throttle=_ingest_throttle(redis, llm.embedding_ingest_rps),
        )
        connector_settings = get_connector_settings()
        sync_service = ConnectorSyncService(
            get_session_maker(),
            OutboundClient(client),
            default_registry(connector_settings),
            SecretBox(connector_settings.keys),
            connector_settings,
        )
        ingest_worker = IngestWorker(get_session_maker(), gateway, rag)
        sync_worker = SyncWorker(get_session_maker(), sync_service)
        try:
            await asyncio.gather(
                ingest_worker.run_forever(stop), sync_worker.run_forever(stop)
            )
        finally:
            if redis is not None:
                await redis.aclose()


def _install_signals(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)


if __name__ == "__main__":
    asyncio.run(main())
