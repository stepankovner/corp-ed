from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import EmailToken


class EmailTokenRepository:
    """Одноразовые ссылки и коды из писем. Хранятся только хеши."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, token: EmailToken) -> EmailToken:
        self.session.add(token)
        await self.session.flush()
        return token

    async def get_by_hash(self, token_hash: str, purpose: str) -> EmailToken | None:
        result = await self.session.scalars(
            select(EmailToken)
            .where(EmailToken.token_hash == token_hash, EmailToken.purpose == purpose)
            .with_for_update()
        )
        return result.first()

    async def latest_active(
        self, account_id: UUID, purpose: str, now: datetime
    ) -> EmailToken | None:
        """Последний неиспользованный и не истёкший токен — для кода из письма."""
        result = await self.session.scalars(
            select(EmailToken)
            .where(
                EmailToken.account_id == account_id,
                EmailToken.purpose == purpose,
                EmailToken.used_at.is_(None),
                EmailToken.expires_at > now,
            )
            .order_by(EmailToken.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        return result.first()

    async def invalidate(self, account_id: UUID, purpose: str, now: datetime) -> None:
        """Погасить прежние токены: действует только последнее письмо."""
        await self.session.execute(
            update(EmailToken)
            .where(
                EmailToken.account_id == account_id,
                EmailToken.purpose == purpose,
                EmailToken.used_at.is_(None),
            )
            .values(used_at=now)
        )
