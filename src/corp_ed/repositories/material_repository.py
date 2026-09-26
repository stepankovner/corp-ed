from collections.abc import Collection, Iterable
from uuid import UUID

from sqlalchemy import delete, select
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
