from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import Invite, InviteLookup


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

    async def get_for_update(self, invite_id: UUID) -> Invite | None:
        """Приглашение с блокировкой строки.

        Два человека по одной ссылке одновременно не должны пройти лимит
        использований: второй ждёт, пока первый допишет uses.
        """
        result = await self.session.scalars(
            select(Invite).where(Invite.id == invite_id).with_for_update()
        )
        return result.first()

    def add_lookup(self, lookup: InviteLookup) -> None:
        self.session.add(lookup)

    async def find_lookup(self, hash_: str) -> InviteLookup | None:
        """Компания и приглашение по хешу ссылки или кода — до того, как
        компания известна (таблица не под RLS, в ней только хеши)."""
        result = await self.session.scalars(
            select(InviteLookup).where(InviteLookup.hash == hash_)
        )
        return result.first()

    async def drop_lookups(self, invite_id: UUID) -> None:
        await self.session.execute(
            delete(InviteLookup).where(InviteLookup.invite_id == invite_id)
        )

    async def list_recent(self, limit: int = 50) -> list[Invite]:
        result = await self.session.scalars(
            select(Invite).order_by(Invite.created_at.desc()).limit(limit)
        )
        return list(result)
