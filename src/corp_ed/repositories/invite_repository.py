from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import Invite


class InviteRepository:
    """Ссылки-приглашения компании из контекста (RLS и хук изоляции)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, invite: Invite) -> Invite:
        self.session.add(invite)
        await self.session.flush()
        return invite

    async def get(self, invite_id: UUID) -> Invite | None:
        result = await self.session.scalars(
            select(Invite).where(Invite.id == invite_id)
        )
        return result.first()

    async def get_by_hash_for_update(self, token_hash: str) -> Invite | None:
        """Ссылка по хешу токена с блокировкой строки.

        Два человека по одной ссылке одновременно не должны пройти лимит
        использований: второй ждёт, пока первый допишет uses.
        """
        result = await self.session.scalars(
            select(Invite).where(Invite.token_hash == token_hash).with_for_update()
        )
        return result.first()

    async def get_by_hash(self, token_hash: str) -> Invite | None:
        result = await self.session.scalars(
            select(Invite).where(Invite.token_hash == token_hash)
        )
        return result.first()

    async def list_recent(self, limit: int = 50) -> list[Invite]:
        result = await self.session.scalars(
            select(Invite).order_by(Invite.created_at.desc()).limit(limit)
        )
        return list(result)
