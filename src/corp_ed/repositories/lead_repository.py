from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import Lead


class LeadRepository:
    """Заявки на созвон. Не тенантские: RLS и хук изоляции их не касаются."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, lead: Lead) -> Lead:
        self.session.add(lead)
        await self.session.flush()
        return lead

    async def get(self, lead_id: UUID) -> Lead | None:
        result = await self.session.scalars(select(Lead).where(Lead.id == lead_id))
        return result.first()

    async def list_recent(self, *, status: str | None, limit: int) -> list[Lead]:
        query = select(Lead).order_by(Lead.created_at.desc()).limit(limit)
        if status is not None:
            query = query.where(Lead.status == status)
        return list(await self.session.scalars(query))

    async def delete_older_than(self, cutoff: datetime) -> int:
        result = await self.session.execute(
            delete(Lead).where(Lead.created_at < cutoff)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]
