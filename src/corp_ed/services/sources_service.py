"""«Где ищет ассистент» глазами сотрудника (ТЗ §5).

Загруженные документы, которые сотрудник видит, — по папкам (правило
видимости то же, что у поиска: ChunkRepository.visible_to), и источники
компании: подключённые администратором и те, что сотрудник подключает
сам (Битрикс24, Яндекс 360), с его состоянием в них.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.tenant_context import require_tenant
from corp_ed.domain.models import Folder, Material, MaterialStatus, User
from corp_ed.domain.types import ConnectorMode, ConnectorStatus
from corp_ed.repositories.chunk_repository import visible_to
from corp_ed.repositories.connector_repository import (
    ConnectorRepository,
    GrantRepository,
)


@dataclass(frozen=True)
class FileGroup:
    folder_id: UUID | None
    name: str
    restricted: bool
    documents: int


@dataclass(frozen=True)
class ConnectorSource:
    id: UUID
    kind: str
    name: str
    mode: str
    working: bool
    """Подключение не остановлено (не пауза и не ошибка учётных данных)."""
    grant_status: str | None
    """Для подключений, которые сотрудник делает сам: его состояние."""


@dataclass(frozen=True)
class MySources:
    files: list[FileGroup]
    connectors: list[ConnectorSource]


class SourcesService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def mine(self, member: User) -> MySources:
        tenant_id = require_tenant()
        rows = await self.session.execute(
            select(Material.folder_id, func.count())
            .where(
                Material.tenant_id == tenant_id,
                Material.connector_id.is_(None),
                Material.status == MaterialStatus.READY,
                visible_to(member.id, tenant_id),
            )
            .group_by(Material.folder_id)
        )
        counts = {folder_id: int(count) for folder_id, count in rows}
        folders = {
            folder.id: folder
            for folder in (
                await self.session.scalars(
                    select(Folder).where(Folder.id.in_([i for i in counts if i]))
                )
            ).all()
        }
        files = (
            [
                FileGroup(
                    folder_id=None,
                    name="Общие документы",
                    restricted=False,
                    documents=counts[None],
                )
            ]
            if counts.get(None)
            else []
        )
        files += sorted(
            (
                FileGroup(
                    folder_id=folder.id,
                    name=folder.name,
                    restricted=folder.restricted,
                    documents=counts[folder.id],
                )
                for folder in folders.values()
            ),
            key=lambda group: group.name.lower(),
        )

        grants = {
            grant.connector_id: grant
            for grant in await GrantRepository(self.session).list_for_user(member.id)
        }
        connectors = [
            ConnectorSource(
                id=connector.id,
                kind=connector.kind,
                name=connector.name,
                mode=connector.mode,
                working=connector.status == ConnectorStatus.ACTIVE.value,
                grant_status=(
                    grants[connector.id].status if connector.id in grants else None
                )
                if connector.mode == ConnectorMode.PER_USER.value
                else None,
            )
            for connector in await ConnectorRepository(self.session).list_all()
        ]
        return MySources(files=files, connectors=connectors)
