from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from corp_ed.domain.models import Program, ProgramStatus


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
        """Программа вместе с брифом.

        Бриф загружается сразу: экран показывает должность стажёра, а
        обращение к связи после выхода из сессии в async-режиме падает.
        """
        return await self.session.get(
            Program,
            program_id,
            options=[selectinload(Program.brief)],
        )

    async def get_approved_for_intern(self, intern_id: UUID) -> Program | None:
        """Утверждённая программа, назначенная этому стажёру."""
        result = await self.session.scalars(
            select(Program)
            .options(selectinload(Program.brief))
            .where(
                Program.intern_id == intern_id,
                Program.status == ProgramStatus.APPROVED,
            )
            .order_by(Program.created_at.desc())
        )
        return result.first()

    async def list_all(self) -> list[Program]:
        """Программы тенанта, новые сверху. Фильтр по тенанту добавит хук."""
        result = await self.session.scalars(
            select(Program)
            .options(selectinload(Program.brief))
            .order_by(Program.created_at.desc())
        )
        return list(result)
