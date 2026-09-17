from uuid import UUID

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
