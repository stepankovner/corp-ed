"""Воркер фоновых задач: python -m corp_ed.worker

Три цикла в одном процессе:
- IngestWorker — задачи ingest_jobs (нарезка, эмбеддинги, замена чанков);
- SyncWorker — задачи connector_sync_jobs (синхронизация коннекторов) и
  планировщик, который раз в минуту ставит в очередь подключения с
  истёкшим интервалом;
- MailWorker — письма из outbox_emails (services/mail_worker.py).

Каждая задача выполняется в контексте своего тенанта, временные сбои
повторяются с растущей паузой. Останавливается по SIGTERM/SIGINT после
текущей задачи — docker stop не рвёт её посередине.

Пульс для healthcheck контейнера — файл HEARTBEAT_PATH, его обновляет
отдельная задача раз в HEARTBEAT_EVERY секунд. Свежий файл значит, что
процесс жив и цикл событий не заблокирован; застрявшую очередь он не
покажет — её признак растущие PENDING в ingest_jobs (docs/DEPLOY.md §10).

Правило изоляции: одна задача — одна свежая сессия и один tenant_scope.
Identity map сессии, пережившей задачу другого тенанта, отдал бы его
объекты мимо фильтра (DECISIONS.md, «session.get обходит фильтр»).
"""

import asyncio
import contextlib
import os
import signal
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import structlog
from prometheus_client import start_http_server
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.registry import default_registry
from corp_ed.core.config import (
    LLMSettings,
    RagSettings,
    get_connector_settings,
    get_http_settings,
    get_mail_settings,
    get_team_notify_settings,
)
from corp_ed.core.database import get_session_maker
from corp_ed.core.exceptions import NotFoundError
from corp_ed.core.logging import configure_logging
from corp_ed.core.mail import build_sender
from corp_ed.core.metrics import WORKER_HEARTBEAT, WORKER_JOBS, WORKER_QUEUE
from corp_ed.core.outbound import OutboundClient
from corp_ed.core.readiness import WORKER_HEARTBEAT_KEY, WORKER_HEARTBEAT_TTL
from corp_ed.core.secrets import SecretBox
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import MaterialStatus
from corp_ed.domain.types import SyncRunStatus, SyncTrigger
from corp_ed.llm.embedding_gateway import EmbeddingGateway
from corp_ed.llm.errors import LLMError
from corp_ed.llm.factory import build_embedding_gateway
from corp_ed.llm.throttle import InMemoryThrottle, RedisThrottle, Throttle
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
from corp_ed.services.mail_worker import MailWorker
from corp_ed.services.team_notify import build_team_notifier
from corp_ed.services.team_notify import drain as drain_team_notifier

logger = structlog.get_logger()

MAX_ATTEMPTS = 5
STALE_AFTER = timedelta(minutes=15)
"""Задача в RUNNING дольше этого — воркер упал; её берёт другой."""
IDLE_SLEEP = 2.0
INGEST_MAX_WAIT = 300.0
"""Сколько задача готова ждать слота квоты эмбеддингов."""
HEARTBEAT_PATH = Path("/tmp/corp-ed-worker.alive")  # noqa: S108 — файл внутри контейнера воркера
"""Файл-пульс; compose.yaml считает воркер живым, пока файлу меньше 120 с."""
HEARTBEAT_EVERY = 30.0

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
            WORKER_JOBS.labels("ingest", "skipped").inc()
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
        WORKER_JOBS.labels("ingest", "done").inc()
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
            result = "retry"
        else:
            await service.mark(job.material_id, MaterialStatus.FAILED, error)
            await jobs.mark_failed(job.id, error)
            result = "failed"
        await session.commit()
        WORKER_JOBS.labels("ingest", result).inc()


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
                result = "done"
            elif retryable and job.attempts < self.max_attempts:
                await jobs.retry_later(job.id, error, retry_delay(job.attempts))
                result = "retry"
            else:
                await jobs.mark_failed(job.id, error)
                result = "failed"
            await session.commit()
        WORKER_JOBS.labels("sync", result).inc()

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


async def heartbeat(
    stop: asyncio.Event,
    path: Path = HEARTBEAT_PATH,
    every: float = HEARTBEAT_EVERY,
    *,
    redis: Redis | None = None,
    session_maker: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Пульс, пока воркер не остановлен.

    Файл — для проверки живости контейнера (compose.yaml); ключ в Redis —
    для /health/ready API (core/readiness.py); метрики — глубина очередей
    и время пульса для Prometheus. Сбой любой части не останавливает
    остальные.
    """
    while not stop.is_set():
        try:
            path.touch()
        except OSError:
            logger.warning("worker_heartbeat_failed", path=str(path))
        if redis is not None:
            try:
                await redis.set(WORKER_HEARTBEAT_KEY, "1", ex=WORKER_HEARTBEAT_TTL)
            except RedisError:
                logger.warning("worker_heartbeat_redis_failed")
        if session_maker is not None:
            await _update_queue_metrics(session_maker)
        WORKER_HEARTBEAT.set_to_current_time()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every)


_QUEUE_COUNTS = {
    "ingest": text("SELECT status, count(*) FROM ingest_jobs GROUP BY status"),
    "sync": text("SELECT status, count(*) FROM connector_sync_jobs GROUP BY status"),
}
"""Очереди не под RLS (воркер берёт задачу до того, как знает компанию),
поэтому счёт по всем компаниям — одним запросом без tenant_scope."""


async def _update_queue_metrics(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    try:
        async with session_maker() as session:
            counts = {
                queue: (await session.execute(query)).all()
                for queue, query in _QUEUE_COUNTS.items()
            }
    except SQLAlchemyError:
        logger.warning("worker_queue_metrics_failed")
        return
    # Статус, из которого задачи ушли, должен пропасть, а не остаться с
    # прошлым числом (в правилах тревог — «or vector(0)»).
    WORKER_QUEUE.clear()
    for queue, rows in counts.items():
        for status, count in rows:
            WORKER_QUEUE.labels(queue, str(status).lower()).set(count)


def _ingest_throttle(redis: Redis | None, rate: float) -> Throttle:
    if redis is None:
        return InMemoryThrottle(rate, max_wait=INGEST_MAX_WAIT)
    return RedisThrottle(redis, "embedding-ingest", rate, max_wait=INGEST_MAX_WAIT)


async def main(install_signals: Callable[[asyncio.Event], None] | None = None) -> None:
    configure_logging()
    llm = LLMSettings()
    rag = RagSettings()  # type: ignore[call-arg]
    http = get_http_settings()

    redis = (
        Redis.from_url(http.redis_url.get_secret_value()) if http.redis_url else None
    )
    # Метрики для Prometheus — внутри сети Docker, порт не публикуется.
    metrics_port = int(os.environ.get("WORKER_METRICS_PORT", "9101"))
    if metrics_port:
        start_http_server(metrics_port)
    stop = asyncio.Event()
    (install_signals or _install_signals)(stop)

    connector_settings = get_connector_settings()
    async with httpx.AsyncClient() as client:
        gateway = build_embedding_gateway(
            client,
            llm,
            document_throttle=_ingest_throttle(redis, llm.embedding_ingest_rps),
        )
        notifier = build_team_notifier(client, get_team_notify_settings())
        sync_service = ConnectorSyncService(
            get_session_maker(),
            OutboundClient(client, via_proxy=connector_settings.outbound_via_proxy),
            default_registry(connector_settings),
            SecretBox(connector_settings.keys),
            connector_settings,
            notifier=notifier,
        )
        ingest_worker = IngestWorker(get_session_maker(), gateway, rag)
        sync_worker = SyncWorker(get_session_maker(), sync_service)
        mail_worker = MailWorker(get_session_maker(), build_sender(get_mail_settings()))
        try:
            await asyncio.gather(
                ingest_worker.run_forever(stop),
                sync_worker.run_forever(stop),
                mail_worker.run_forever(stop),
                heartbeat(stop, redis=redis, session_maker=get_session_maker()),
            )
        finally:
            await drain_team_notifier(notifier)
            if redis is not None:
                await redis.aclose()


def _install_signals(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)


if __name__ == "__main__":
    asyncio.run(main())
