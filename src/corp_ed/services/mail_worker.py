"""Отправка писем из очереди outbox_emails — цикл воркера (ТЗ §3).

Письмо берётся с арендой (claim_next): упавший посреди отправки воркер
не держит его вечно. Временная ошибка — повтор с растущей паузой,
постоянная (неверный пароль ящика, адрес отвергнут) — письмо помечается
неотправленным. Отправленные и неотправленные удаляются через
RETENTION.
"""

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.mail import MailDeliveryError, MailSender, OutgoingEmail
from corp_ed.repositories.outbox_repository import OutboxRepository

logger = structlog.get_logger()

IDLE_SLEEP = 2.0
"""Пауза при пустой очереди: код из письма должен прийти за секунды."""
LEASE = timedelta(minutes=2)
MAX_ATTEMPTS = 8
RETENTION = timedelta(days=30)
PURGE_EVERY = timedelta(hours=6)


def retry_delay(attempt: int) -> timedelta:
    """30 с, 1 мин, 2 мин … но не больше часа."""
    return min(timedelta(seconds=30 * 2 ** (attempt - 1)), timedelta(hours=1))


class MailWorker:
    def __init__(
        self, session_maker: async_sessionmaker[AsyncSession], sender: MailSender
    ) -> None:
        self.session_maker = session_maker
        self.sender = sender
        self._last_purge: datetime | None = None

    async def run_once(self) -> bool:
        """Отправить одно письмо. False — очередь пуста."""
        now = _now()
        async with self.session_maker() as session:
            claimed = await OutboxRepository(session).claim_next(now, LEASE)
            await session.commit()
        if claimed is None:
            return False

        log = logger.bind(email_id=str(claimed.id), attempt=claimed.attempts)
        try:
            await self.sender.send(
                OutgoingEmail(
                    to=claimed.to_email,
                    subject=claimed.subject,
                    text=claimed.text_body,
                    html=claimed.html_body,
                )
            )
        except MailDeliveryError as exc:
            await self._fail(claimed.id, claimed.attempts, exc.code, exc.retryable)
            log.warning("mail_failed", error=exc.code, retryable=exc.retryable)
            return True
        except Exception:
            await self._fail(claimed.id, claimed.attempts, "internal", True)
            log.exception("mail_failed")
            return True

        async with self.session_maker() as session:
            await OutboxRepository(session).mark_sent(claimed.id, _now())
            await session.commit()
        log.info("mail_sent")
        return True

    async def purge(self) -> None:
        now = _now()
        if self._last_purge and now - self._last_purge < PURGE_EVERY:
            return
        async with self.session_maker() as session:
            removed = await OutboxRepository(session).purge_before(now - RETENTION)
            await session.commit()
        self._last_purge = now
        if removed:
            logger.info("mail_outbox_purged", removed=removed)

    async def run_forever(self, stop: asyncio.Event) -> None:
        logger.info("mail_worker_started")
        while not stop.is_set():
            try:
                worked = await self.run_once()
                if not worked:
                    await self.purge()
            except Exception:
                logger.exception("mail_worker_loop_error")
                worked = False
            if not worked:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=IDLE_SLEEP)
        logger.info("mail_worker_stopped")

    async def _fail(
        self, email_id: UUID, attempts: int, code: str, retryable: bool
    ) -> None:
        async with self.session_maker() as session:
            repo = OutboxRepository(session)
            if retryable and attempts < MAX_ATTEMPTS:
                await repo.mark_retry(
                    email_id,
                    _now() + retry_delay(attempts),
                    code,
                )
            else:
                await repo.mark_failed(email_id, _now(), code)
            await session.commit()


def _now() -> datetime:
    return datetime.now(UTC)
