"""Справочник коллег (ТЗ §4): кто работает в компании, должность, отдел,
контакты. Видят только люди этой компании — список строится под RLS
тенанта из токена; вне компании профиля не видно никому."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import NotFoundError, PermissionError
from corp_ed.domain.models import Department, MemberStatus, User, UserRole
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.repositories.department_repository import DepartmentRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services.avatar_service import AvatarService, avatar_url


class _Unset:
    """Поле не пришло в запросе (в отличие от null — «очистить»)."""


UNSET = _Unset()


@dataclass(frozen=True)
class Person:
    member: User
    department: Department | None
    avatar_url: str | None


class PeopleService:
    def __init__(
        self,
        users: UserRepository,
        departments: DepartmentRepository,
        avatars: AvatarService,
        audit: AuditRepository,
        session: AsyncSession,
    ) -> None:
        self.users = users
        self.departments = departments
        self.avatars = avatars
        self.audit = audit
        self.session = session

    async def list_people(self) -> list[Person]:
        members = await self.users.list_people()
        return await self._people(members)

    async def get_person(self, member_id: UUID) -> Person:
        member = await self.users.get_by_id(member_id)
        if (
            member is None
            or member.status is not MemberStatus.ACTIVE
            or not member.account
        ):
            raise NotFoundError("Сотрудник не найден")
        [person] = await self._people([member])
        return person

    async def update_work(
        self,
        actor: User,
        member_id: UUID,
        *,
        position: str | None | _Unset = UNSET,
        department_id: UUID | None | _Unset = UNSET,
    ) -> Person:
        """Должность и отдел в компании: свои — сам человек, чужие —
        администратор (ТЗ §4). Правка администратора — в журнал."""
        if member_id != actor.id and actor.role is not UserRole.ADMIN:
            raise PermissionError("Должность и отдел коллеги меняет администратор")
        member = await self.users.get_by_id(member_id)
        if member is None or member.status is not MemberStatus.ACTIVE:
            raise NotFoundError("Сотрудник не найден")
        before = {
            "position": member.position,
            "department_id": _str(member.department_id),
        }
        if not isinstance(position, _Unset):
            member.position = position
        if isinstance(department_id, UUID):
            # Поиск под RLS: отдел чужой компании не найдётся.
            if await self.departments.get_by_id(department_id) is None:
                raise NotFoundError("Отдел не найден")
            member.department_id = department_id
        elif department_id is None:
            member.department_id = None
        after = {
            "position": member.position,
            "department_id": _str(member.department_id),
        }
        if member.id != actor.id and before != after:
            self.audit.record(
                AuditAction.USER_PROFILE_UPDATED,
                tenant_id=member.tenant_id,
                actor_id=actor.id,
                target_type="user",
                target_id=member.id,
                details={"before": before, "after": after},
            )
        await self.session.commit()
        return await self.get_person(member.id)

    async def _people(self, members: list[User]) -> list[Person]:
        departments = {item.id: item for item in await self.departments.list_all()}
        account_ids = [m.account_id for m in members if m.account_id is not None]
        versions = await self.avatars.versions(account_ids)
        people: list[Person] = []
        for member in members:
            version = versions.get(member.account_id) if member.account_id else None
            people.append(
                Person(
                    member=member,
                    department=departments.get(member.department_id)
                    if member.department_id
                    else None,
                    avatar_url=avatar_url(member.account_id, version)
                    if member.account_id and version
                    else None,
                )
            )
        return people


def _str(value: UUID | None) -> str | None:
    return str(value) if value else None
