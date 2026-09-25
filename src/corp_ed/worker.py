"""Воркер фонового ингеста: python -m corp_ed.worker

Берёт задачи из ingest_jobs по одной, выполняет в контексте тенанта
задачи, повторяет временные сбои с растущей паузой. Останавливается по
SIGTERM/SIGINT после текущей задачи — docker stop не рвёт её посередине.

Правило изоляции: одна задача — одна свежая сессия и один tenant_scope.
Identity map сессии, пережившей задачу другого тенанта, отдал бы его
объекты мимо фильтра (DECISIONS.md, «session.get обходит фильтр»).
"""

import asyncio
import contextlib
import signal
from collections.abc import Callable
from datetime import timedelta

import httpx
import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.config import LLMSettings, RagSettings, get_http_settings
from corp_ed.core.database import get_session_maker
from corp_ed.core.exceptions import NotFoundError
from corp_ed.core.logging import configure_logging
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import MaterialStatus
from corp_ed.llm.embedding_gateway import EmbeddingGateway
from corp_ed.llm.errors import LLMError
from corp_ed.llm.throttle import InMemoryThrottle, RedisThrottle, Throttle
from corp_ed.llm.yandex_embedding import YandexEmbeddingAdapter
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.repositories.ingest_job_repository import (
    ClaimedJob,
    IngestJobRepository,
)
from corp_ed.repositories.material_repository import MaterialRepository
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
            document_throttle=_ingest_throttle(redis, llm.embedding_ingest_rps),
        )
        worker = IngestWorker(get_session_maker(), gateway, rag)
        try:
            await worker.run_forever(stop)
        finally:
            if redis is not None:
                await redis.aclose()


def _install_signals(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)


if __name__ == "__main__":
    asyncio.run(main())
