from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.tenant_context import require_tenant
from corp_ed.domain.models import (
    Department,
    Folder,
    FolderDepartment,
    MemberStatus,
    User,
)


class DepartmentRepository:
    """Отделы компании (ТЗ §7). Выборки через ORM фильтрует хук изоляции,
    колоночные — явным tenant_id (как в GlossaryRepository)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self) -> list[Department]:
        result = await self.session.scalars(
            select(Department).order_by(func.lower(Department.name))
        )
        return list(result)

    async def member_counts(self) -> dict[UUID, int]:
        """Сколько работающих людей в каждом отделе."""
        result = await self.session.execute(
            select(User.department_id, func.count())
            .where(
                User.tenant_id == require_tenant(),
                User.status == MemberStatus.ACTIVE,
                User.department_id.is_not(None),
            )
            .group_by(User.department_id)
        )
        return {row[0]: int(row[1]) for row in result}

    async def get_by_id(self, department_id: UUID) -> Department | None:
        # select, а не session.get: см. UserRepository.get_by_id.
        result = await self.session.scalars(
            select(Department).where(Department.id == department_id)
        )
        return result.first()

    async def opens_restricted_folder(self, department_id: UUID) -> bool:
        """Открыта ли отделу хоть одна закрытая папка."""
        result = await self.session.scalar(
            select(func.count())
            .select_from(FolderDepartment)
            .join(Folder, Folder.id == FolderDepartment.folder_id)
            .where(
                FolderDepartment.tenant_id == require_tenant(),
                FolderDepartment.department_id == department_id,
                Folder.restricted.is_(True),
            )
        )
        return bool(result)

    async def find_by_name(self, name: str) -> Department | None:
        result = await self.session.scalars(
            select(Department).where(func.lower(Department.name) == name.lower())
        )
        return result.first()

    async def count(self) -> int:
        result = await self.session.scalar(
            select(func.count())
            .select_from(Department)
            .where(Department.tenant_id == require_tenant())
        )
        return int(result or 0)

    async def create(self, department: Department) -> Department:
        self.session.add(department)
        await self.session.flush()
        await self.session.refresh(department)
        return department

    async def delete(self, department: Department) -> None:
        await self.session.delete(department)
