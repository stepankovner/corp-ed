from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import Brief


class BriefRepository:
    """Доступ к данным брифов в БД."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, brief_id: UUID) -> Brief | None:
        return await self.session.get(Brief, brief_id)
