from collections.abc import Collection, Iterable
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.tenant_context import require_tenant
from corp_ed.domain.models import Material, MaterialAccess


class MaterialRepository:
    """Доступ к данным материалов в БД."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, material_id: UUID) -> Material | None:
        # select, а не session.get: см. UserRepository.get_by_id.
        result = await self.session.scalars(
            select(Material).where(Material.id == material_id)
        )
        return result.first()

    async def create(self, material: Material) -> Material:
        self.session.add(material)
        await self.session.flush()
        await self.session.refresh(material)
        return material

    async def list_all(self) -> list[Material]:
        result = await self.session.scalars(
            select(Material).order_by(Material.created_at.desc())
        )
        return list(result)

    async def titles_in_search(self) -> dict[UUID, str]:
        """id → название документов, которые участвуют в поиске (не
        заменены, BH-41) — для подсказки «новая редакция». Без content:
        текст всех документов компании здесь не нужен.

        Колоночный select — хук изоляции его не видит, фильтр явный.
        """
        rows = await self.session.execute(
            select(Material.id, Material.title).where(
                Material.tenant_id == require_tenant(),
                Material.superseded_at.is_(None),
            )
        )
        return {row.id: row.title for row in rows}

    async def directories(self) -> list[tuple[UUID, str, str]]:
        """Справочники компании (BH-47): id, название, текст документов,
        которые администратор отметил справочником и которые в поиске."""
        rows = await self.session.execute(
            select(Material.id, Material.title, Material.content)
            .where(
                Material.tenant_id == require_tenant(),
                Material.is_directory.is_(True),
                Material.superseded_at.is_(None),
            )
            .order_by(Material.created_at)
        )
        return [(row.id, row.title, row.content) for row in rows]

    async def get_by_sha256(self, sha256: str) -> Material | None:
        """Дубль среди РУЧНЫХ загрузок: документ коннектора с тем же
        содержимым — другой документ с другой ссылкой."""
        result = await self.session.scalars(
            select(Material).where(
                Material.source_sha256 == sha256, Material.connector_id.is_(None)
            )
        )
        return result.first()

    async def get_by_external_id(
        self, connector_id: UUID, external_id: str
    ) -> Material | None:
        result = await self.session.scalars(
            select(Material).where(
                Material.connector_id == connector_id,
                Material.external_id == external_id,
            )
        )
        return result.first()

    async def delete_by_connector_except(
        self, connector_id: UUID, keep_external_ids: Collection[str]
    ) -> int:
        """Удалить документы коннектора, которых больше нет в источнике.

        Bulk DELETE идёт мимо ORM-хуков — фильтр по тенанту явный. Чанки,
        задачи и права уходят каскадом.
        """
        stmt = delete(Material).where(
            Material.tenant_id == require_tenant(),
            Material.connector_id == connector_id,
        )
        if keep_external_ids:
            stmt = stmt.where(Material.external_id.not_in(list(keep_external_ids)))
        result = await self.session.execute(stmt)
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def delete_by_connector_synced_before(
        self, connector_id: UUID, before: datetime
    ) -> int:
        """Удалить документы коннектора, которых не коснулся завершённый
        обход агента (synced_at раньше его начала, docs/SMB-AGENT.md §3.5).
        Чанки, задачи и права уходят каскадом."""
        result = await self.session.execute(
            delete(Material).where(
                Material.tenant_id == require_tenant(),
                Material.connector_id == connector_id,
                or_(Material.synced_at.is_(None), Material.synced_at < before),
            )
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def external_versions(
        self, connector_id: UUID, *, after: str | None, limit: int
    ) -> list[tuple[str, str]]:
        """(external_id, external_version) документов коннектора по порядку
        external_id, начиная после after: что уже есть в kronto."""
        stmt = (
            select(Material.external_id, Material.external_version)
            .where(
                Material.tenant_id == require_tenant(),
                Material.connector_id == connector_id,
                Material.external_id.is_not(None),
            )
            .order_by(Material.external_id)
            .limit(limit)
        )
        if after is not None:
            stmt = stmt.where(Material.external_id > after)
        rows = await self.session.execute(stmt)
        return [(str(eid), str(version or "")) for eid, version in rows]

    # --- material_access: кто видит документ с visibility = restricted ---

    async def replace_access(self, material_id: UUID, user_ids: Iterable[UUID]) -> None:
        """Заменить список сотрудников, которым виден документ (режим
        organization: ACL источника целиком)."""
        tenant_id = require_tenant()
        await self.session.execute(
            delete(MaterialAccess).where(
                MaterialAccess.tenant_id == tenant_id,
                MaterialAccess.material_id == material_id,
            )
        )
        rows = [
            {"tenant_id": tenant_id, "material_id": material_id, "user_id": user_id}
            for user_id in set(user_ids)
        ]
        if rows:
            await self.session.execute(insert(MaterialAccess).values(rows))

    async def grant_access(self, material_id: UUID, user_id: UUID) -> None:
        """Добавить сотрудника (режим per_user: документ есть в его листинге)."""
        await self.session.execute(
            insert(MaterialAccess)
            .values(
                tenant_id=require_tenant(), material_id=material_id, user_id=user_id
            )
            .on_conflict_do_nothing()
        )

    async def revoke_access_except(
        self, connector_id: UUID, user_id: UUID, keep_material_ids: Collection[UUID]
    ) -> int:
        """Убрать у сотрудника документы коннектора, которых нет в его
        листинге: права в источнике отозвали — и у нас пропали."""
        tenant_id = require_tenant()
        connector_materials = select(Material.id).where(
            Material.tenant_id == tenant_id, Material.connector_id == connector_id
        )
        stmt = delete(MaterialAccess).where(
            MaterialAccess.tenant_id == tenant_id,
            MaterialAccess.user_id == user_id,
            MaterialAccess.material_id.in_(connector_materials),
        )
        if keep_material_ids:
            stmt = stmt.where(
                MaterialAccess.material_id.not_in(list(keep_material_ids))
            )
        result = await self.session.execute(stmt)
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def revoke_all_access(self, connector_id: UUID, user_id: UUID) -> int:
        return await self.revoke_access_except(connector_id, user_id, ())

    async def delete(self, material: Material) -> None:
        # Чанки и задачи удаляет база: ON DELETE CASCADE на внешних ключах.
        await self.session.delete(material)
        await self.session.flush()
