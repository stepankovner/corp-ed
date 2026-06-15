from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lms.core.tenant_context import current_tenant
from lms.domain.models import Course


class CourseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, title: str) -> Course:
        tenant_id = current_tenant.get()
        course = Course(title=title, tenant_id=tenant_id)
        self.session.add(course)
        await self.session.commit()
        await self.session.refresh(course)
        return course

    async def list_all(self) -> Sequence[Course]:
        # tenant-фильтр подмешает хук автоматически — здесь его НЕ пишем
        result = await self.session.execute(select(Course))
        return result.scalars().all()
