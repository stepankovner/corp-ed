from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import RefreshToken


class RefreshTokenRepository:
    """Хранилище refresh-токенов.

    Таблица не тенант-скоупная (см. RefreshToken): выборка по хешу
    токена, которого у атакующего нет. Отзыв — bulk UPDATE по
    пользователю или семье, фильтр по тенанту здесь не нужен: ключ
    уже однозначно принадлежит одному тенанту.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, token: RefreshToken) -> None:
        self.session.add(token)
        await self.session.flush()

    async def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        result = await self.session.execute(
            # FOR UPDATE: два параллельных запроса с одним токеном не
            # должны оба пройти ротацию — второй дождётся первого и
            # увидит used_at.
            select(RefreshToken)
            .where(RefreshToken.token_hash == token_hash)
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def revoke_family(self, family_id: UUID, now: datetime) -> None:
        await self.session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None)
            )
            .values(revoked_at=now)
        )

    async def revoke_user(self, user_id: UUID, now: datetime) -> None:
        await self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
