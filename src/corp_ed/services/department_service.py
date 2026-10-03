from uuid import UUID

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import ConflictError, DomainError, NotFoundError
from corp_ed.domain.models import Department, User
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.repositories.department_repository import DepartmentRepository

logger = structlog.get_logger()

MAX_DEPARTMENTS = 200
"""Потолок на компанию: отделы — выпадающий список в профиле, а не
справочник номенклатуры."""


class DepartmentLimitError(DomainError):
    def __init__(self) -> None:
        super().__init__(f"В компании не больше {MAX_DEPARTMENTS} отделов")


def _duplicate(name: str) -> ConflictError:
    return ConflictError(f"Отдел «{name}» уже есть")


class DepartmentService:
    """Отделы компании (ТЗ §7): заводит и переименовывает администратор,
    человек выбирает свой в профиле. Удалённый отдел пропадает у людей
    (ON DELETE SET NULL), сами люди остаются."""

    def __init__(
        self,
        repository: DepartmentRepository,
        audit: AuditRepository,
        session: AsyncSession,
    ) -> None:
        self.repository = repository
        self.audit = audit
        self.session = session

    async def list_with_counts(self) -> list[tuple[Department, int]]:
        departments = await self.repository.list_all()
        counts = await self.repository.member_counts()
        return [(item, counts.get(item.id, 0)) for item in departments]

    async def create(self, actor: User, name: str) -> Department:
        if await self.repository.count() >= MAX_DEPARTMENTS:
            raise DepartmentLimitError()
        await self._ensure_unique(name)
        try:
            department = await self.repository.create(Department(name=name))
            self._record(AuditAction.DEPARTMENT_CREATED, actor, department)
            await self.session.commit()
        except IntegrityError as exc:
            # Два одинаковых названия одновременно: развёл уникальный индекс.
            await self.session.rollback()
            raise _duplicate(name) from exc
        return department

    async def rename(self, actor: User, department_id: UUID, name: str) -> Department:
        department = await self.get(department_id)
        if name.lower() != department.name.lower():
            await self._ensure_unique(name)
        previous = department.name
        department.name = name
        self._record(AuditAction.DEPARTMENT_UPDATED, actor, department, previous)
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise _duplicate(name) from exc
        await self.session.refresh(department)
        return department

    async def delete(self, actor: User, department_id: UUID) -> None:
        department = await self.get(department_id)
        self._record(AuditAction.DEPARTMENT_DELETED, actor, department)
        await self.repository.delete(department)
        await self.session.commit()

    async def get(self, department_id: UUID) -> Department:
        department = await self.repository.get_by_id(department_id)
        if department is None:
            raise NotFoundError("Отдел не найден")
        return department

    async def _ensure_unique(self, name: str) -> None:
        if await self.repository.find_by_name(name) is not None:
            raise _duplicate(name)

    def _record(
        self,
        action: AuditAction,
        actor: User,
        department: Department,
        previous: str | None = None,
    ) -> None:
        details: dict[str, object] = {"name": department.name}
        if previous is not None:
            details["previous"] = previous
        self.audit.record(
            action,
            tenant_id=department.tenant_id,
            actor_id=actor.id,
            target_type="department",
            target_id=department.id,
            details=details,
        )
