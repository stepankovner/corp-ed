from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import Material


class MaterialRepository:
    """Доступ к данным материалов в БД."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, material_id: UUID) -> Material | None:
        return await self.session.get(Material, material_id)

    async def create(self, material: Material) -> Material:
        self.session.add(material)
        await self.session.flush()
        await self.session.refresh(material)
        return material

    async def list_all(self) -> list[Material]:
        """Материалы тенанта, новые сверху.

        Фильтр по тенанту не пишется руками: это ORM-select сущности,
        его добавит хук _apply_tenant_filter. Сортировка задана явно —
        без order_by порядок строк не определён.
        """
        result = await self.session.scalars(
            select(Material).order_by(Material.created_at.desc())
        )
        return list(result)
