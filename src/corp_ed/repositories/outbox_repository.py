from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import OutboxEmail


@dataclass(frozen=True)
class ClaimedEmail:
    id: UUID
    to_email: str
    subject: str
    text_body: str
    html_body: str
    attempts: int


class OutboxRepository:
    """Очередь писем (ТЗ §3). Пишет запрос, отправляет воркер."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, email: OutboxEmail) -> None:
        self.session.add(email)

    async def claim_next(self, now: datetime, lease: timedelta) -> ClaimedEmail | None:
        """Взять письмо и отодвинуть его следующую попытку на время аренды:
        упавший посреди отправки воркер не держит письмо вечно, а второй
        воркер не отправит его дважды (SKIP LOCKED)."""
        email = (
            await self.session.scalars(
                select(OutboxEmail)
                .where(
                    OutboxEmail.sent_at.is_(None),
                    OutboxEmail.failed_at.is_(None),
                    OutboxEmail.next_attempt_at <= now,
                )
                .order_by(OutboxEmail.next_attempt_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).first()
        if email is None:
            return None
        email.attempts += 1
        email.next_attempt_at = now + lease
        return ClaimedEmail(
            id=email.id,
            to_email=email.to_email,
            subject=email.subject,
            text_body=email.text_body,
            html_body=email.html_body,
            attempts=email.attempts,
        )

    async def mark_sent(self, email_id: UUID, now: datetime) -> None:
        # Текст стирается: в нём ссылки и коды, после отправки они в базе
        # не нужны.
        await self.session.execute(
            update(OutboxEmail)
            .where(OutboxEmail.id == email_id)
            .values(sent_at=now, text_body="", html_body="", last_error=None)
        )

    async def mark_retry(
        self, email_id: UUID, next_attempt_at: datetime, error: str
    ) -> None:
        await self.session.execute(
            update(OutboxEmail)
            .where(OutboxEmail.id == email_id)
            .values(next_attempt_at=next_attempt_at, last_error=error)
        )

    async def mark_failed(self, email_id: UUID, now: datetime, error: str) -> None:
        await self.session.execute(
            update(OutboxEmail)
            .where(OutboxEmail.id == email_id)
            .values(failed_at=now, last_error=error, text_body="", html_body="")
        )

    async def purge_before(self, before: datetime) -> int:
        result = await self.session.execute(
            delete(OutboxEmail).where(OutboxEmail.created_at < before)
        )
        return int(getattr(result, "rowcount", 0) or 0)
