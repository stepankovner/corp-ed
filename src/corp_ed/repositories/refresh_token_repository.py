from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import RefreshToken


class RefreshTokenRepository:
    """Хранилище refresh-токенов.

    Таблица не тенант-скоупная (см. RefreshToken): выборка по хешу
    токена, которого у атакующего нет. Отзыв — bulk UPDATE по учётке
    или семье: вход один на человека, а не на компанию.
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

    async def family_revoked(self, family_id: UUID) -> bool:
        """Сеанс закрыт: «Выйти», «Завершить» в списке сеансов, повтор
        украденного токена, «выйти везде». revoked_at пишут только
        revoke_family и revoke_account, и всей цепочке сразу, поэтому одна
        отозванная запись значит закрытый сеанс. Сеанс без записей — не
        отозван, поэтому отозванные записи нельзя удалять раньше, чем
        истекут access-токены (ACCESS_TOKEN_TTL_MINUTES): сейчас их не
        удаляет никто, кроме удаления самой учётки."""
        result = await self.session.execute(
            select(RefreshToken.id)
            .where(
                RefreshToken.family_id == family_id,
                RefreshToken.revoked_at.is_not(None),
            )
            .limit(1)
        )
        return result.first() is not None

    async def revoke_family(self, family_id: UUID, now: datetime) -> None:
        await self.session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None)
            )
            .values(revoked_at=now)
        )

    async def revoke_account(self, account_id: UUID, now: datetime) -> None:
        await self.session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.account_id == account_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
