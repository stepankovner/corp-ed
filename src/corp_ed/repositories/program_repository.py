from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import Program


class ProgramRepository:
    """Доступ к данным программ в БД."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, program: Program) -> Program:
        self.session.add(program)
        await self.session.flush()
        await self.session.refresh(program)
        return program

    async def get_by_id(self, program_id: UUID) -> Program | None:
        return await self.session.get(Program, program_id)
