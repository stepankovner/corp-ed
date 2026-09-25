from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import Material


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
        result = await self.session.scalars(
            select(Material).where(Material.source_sha256 == sha256)
        )
        return result.first()

    async def delete(self, material: Material) -> None:
        # Чанки и задачи удаляет база: ON DELETE CASCADE на внешних ключах.
        await self.session.delete(material)
        await self.session.flush()
