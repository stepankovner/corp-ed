from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.tenant_context import require_tenant
from corp_ed.domain.models import GlossaryTerm


class GlossaryRepository:
    """Словарь сокращений компании."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self) -> list[GlossaryTerm]:
        result = await self.session.scalars(
            select(GlossaryTerm).order_by(func.lower(GlossaryTerm.term))
        )
        return list(result)

    async def get_by_id(self, term_id: UUID) -> GlossaryTerm | None:
        # select, а не session.get: см. UserRepository.get_by_id.
        result = await self.session.scalars(
            select(GlossaryTerm).where(GlossaryTerm.id == term_id)
        )
        return result.first()

    async def find_by_term(self, term: str) -> GlossaryTerm | None:
        result = await self.session.scalars(
            select(GlossaryTerm).where(
                func.lower(GlossaryTerm.term) == term.strip().lower()
            )
        )
        return result.first()

    async def count(self) -> int:
        # Колоночный select: хук изоляции его не видит, фильтр явный.
        result = await self.session.scalar(
            select(func.count())
            .select_from(GlossaryTerm)
            .where(GlossaryTerm.tenant_id == require_tenant())
        )
        return int(result or 0)

    async def as_mapping(self) -> dict[str, str]:
        """{термин: расшифровка} для expand_query.

        Колоночный select — фильтр по тенанту явный: словарь другой
        компании в вопрос попасть не должен.
        """
        result = await self.session.execute(
            select(GlossaryTerm.term, GlossaryTerm.expansion).where(
                GlossaryTerm.tenant_id == require_tenant()
            )
        )
        return {row.term: row.expansion for row in result}

    async def create(self, entry: GlossaryTerm) -> GlossaryTerm:
        self.session.add(entry)
        await self.session.flush()
        await self.session.refresh(entry)
        return entry

    async def delete(self, entry: GlossaryTerm) -> None:
        await self.session.delete(entry)
        await self.session.flush()
