"""Папки загруженных документов с доступом по отделам (ТЗ §5, §7).

Открытая папка — документы видят все сотрудники; закрытая — только
выбранные отделы и администраторы компании. Правило применяет поиск
(ChunkRepository._visible_to): документ закрытой папки не попадёт ни в
ответ, ни в источники, ни в ссылку «поделиться» для того, кому папка
закрыта.

Документы из источников (коннекторы) в папки не кладутся: их видимость
задают права в самом источнике. Непустую папку не удалить — иначе
документы закрытой папки стали бы видны всем.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import CodedConflictError, NotFoundError
from corp_ed.core.tenant_context import require_tenant
from corp_ed.domain.models import Department, Folder, FolderDepartment, Material, User
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository

logger = structlog.get_logger()

MAX_FOLDERS = 100


class _Unset:
    pass


UNSET = _Unset()


@dataclass(frozen=True)
class FolderView:
    folder: Folder
    departments: list[Department]
    documents: int


class FolderService:
    def __init__(self, session: AsyncSession, audit: AuditRepository) -> None:
        self.session = session
        self.audit = audit

    async def all(self) -> list[FolderView]:
        folders = list(
            (
                await self.session.scalars(
                    select(Folder).order_by(func.lower(Folder.name))
                )
            ).all()
        )
        return await self._views(folders)

    async def create(
        self,
        actor: User,
        *,
        name: str,
        restricted: bool,
        department_ids: Sequence[UUID],
    ) -> FolderView:
        count = await self.session.scalar(
            select(func.count()).where(Folder.tenant_id == require_tenant())
        )
        if (count or 0) >= MAX_FOLDERS:
            raise CodedConflictError(
                f"Папок — не больше {MAX_FOLDERS}", "folders_limit"
            )
        folder = Folder(name=_clean(name), restricted=restricted)
        self.session.add(folder)
        await self._flush_unique()
        await self._set_departments(folder, department_ids if restricted else [])
        self._audit(AuditAction.FOLDER_CREATED, actor, folder)
        await self.session.commit()
        return (await self._views([folder]))[0]

    async def update(
        self,
        actor: User,
        folder_id: UUID,
        *,
        name: str | _Unset = UNSET,
        restricted: bool | _Unset = UNSET,
        department_ids: Sequence[UUID] | _Unset = UNSET,
    ) -> FolderView:
        folder = await self._get(folder_id)
        if not isinstance(name, _Unset):
            folder.name = _clean(name)
        if not isinstance(restricted, _Unset):
            folder.restricted = restricted
        await self._flush_unique()
        if not folder.restricted:
            await self._set_departments(folder, [])
        elif not isinstance(department_ids, _Unset):
            await self._set_departments(folder, department_ids)
        self._audit(AuditAction.FOLDER_UPDATED, actor, folder)
        await self.session.commit()
        return (await self._views([folder]))[0]

    async def delete(self, actor: User, folder_id: UUID) -> None:
        folder = await self._get(folder_id)
        documents = await self.session.scalar(
            select(func.count()).where(
                Material.tenant_id == require_tenant(), Material.folder_id == folder.id
            )
        )
        if documents:
            raise CodedConflictError(
                "В папке есть документы — сначала перенесите или удалите их",
                "folder_not_empty",
            )
        self._audit(AuditAction.FOLDER_DELETED, actor, folder)
        await self.session.delete(folder)
        await self.session.commit()

    async def get(self, folder_id: UUID) -> Folder:
        return await self._get(folder_id)

    # --- внутреннее -------------------------------------------------------------

    async def _get(self, folder_id: UUID) -> Folder:
        folder = (
            await self.session.scalars(select(Folder).where(Folder.id == folder_id))
        ).first()
        if folder is None:
            raise NotFoundError("Папка не найдена")
        return folder

    async def _flush_unique(self) -> None:
        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            raise CodedConflictError(
                "Папка с таким названием уже есть", "folder_exists"
            ) from exc

    async def _set_departments(
        self, folder: Folder, department_ids: Sequence[UUID]
    ) -> None:
        wanted = set(department_ids)
        if wanted:
            found = set(
                (
                    await self.session.scalars(
                        select(Department.id).where(Department.id.in_(wanted))
                    )
                ).all()
            )
            if found != wanted:
                # Чужой или удалённый отдел — под RLS его не видно.
                raise NotFoundError("Отдел не найден")
        await self.session.execute(
            delete(FolderDepartment).where(
                FolderDepartment.tenant_id == require_tenant(),
                FolderDepartment.folder_id == folder.id,
            )
        )
        for department_id in sorted(wanted):
            self.session.add(
                FolderDepartment(folder_id=folder.id, department_id=department_id)
            )
        await self.session.flush()

    async def _views(self, folders: list[Folder]) -> list[FolderView]:
        if not folders:
            return []
        ids = [folder.id for folder in folders]
        tenant_id = require_tenant()
        rows = await self.session.execute(
            select(Material.folder_id, func.count())
            .where(Material.tenant_id == tenant_id, Material.folder_id.in_(ids))
            .group_by(Material.folder_id)
        )
        counts: dict[UUID, int] = {
            folder_id: int(total) for folder_id, total in rows if folder_id
        }
        links = (
            await self.session.execute(
                select(FolderDepartment.folder_id, Department)
                .join(Department, Department.id == FolderDepartment.department_id)
                .where(
                    FolderDepartment.tenant_id == tenant_id,
                    FolderDepartment.folder_id.in_(ids),
                )
                .order_by(func.lower(Department.name))
            )
        ).all()
        departments: dict[UUID, list[Department]] = {i: [] for i in ids}
        for folder_id, department in links:
            departments[folder_id].append(department)
        return [
            FolderView(
                folder=folder,
                departments=departments[folder.id],
                documents=counts.get(folder.id, 0),
            )
            for folder in folders
        ]

    def _audit(self, action: AuditAction, actor: User, folder: Folder) -> None:
        self.audit.record(
            action,
            tenant_id=actor.tenant_id,
            actor_id=actor.id,
            target_type="folder",
            target_id=folder.id,
            details={"name": folder.name, "restricted": folder.restricted},
        )


def _clean(name: str) -> str:
    return " ".join(name.split())[:100]
