from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.tenant_context import require_tenant
from corp_ed.domain.models import Chunk
from corp_ed.domain.types import ChunkMatch


class ChunkRepository:
    """Доступ к данным чанков в БД."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def bulk_create(self, chunks: list[Chunk]) -> None:
        self.session.add_all(chunks)
        await self.session.flush()

    async def delete_by_material(self, material_id: UUID) -> None:
        """Удалить все чанки материала.

        Фильтр по тенанту здесь обязателен: bulk DELETE идёт мимо
        обоих хуков изоляции — _apply_tenant_filter реагирует только
        на SELECT, _check_tenant_on_write работает с объектами сессии.
        """
        stmt = delete(Chunk).where(
            Chunk.material_id == material_id,
            Chunk.tenant_id == require_tenant(),
        )
        await self.session.execute(stmt)

    async def search(self, embedding: list[float], limit: int = 5) -> list[ChunkMatch]:
        tenant_id = require_tenant()

        distance = Chunk.embedding.cosine_distance(embedding)

        stmt = (
            select(
                Chunk.id,
                Chunk.content,
                Chunk.material_id,
                Chunk.position,
                distance.label("distance"),
            )
            # Фильтр обязателен: hook вешает with_loader_criteria, а он
            # применяется к загрузке ORM-сущностей. Здесь колоночный
            # select, сущность не грузится — автоматики нет.
            .where(Chunk.tenant_id == tenant_id)
            .order_by(distance)
            .limit(limit)
        )

        result = await self.session.execute(stmt)

        return [
            ChunkMatch(
                id=row.id,
                content=row.content,
                material_id=row.material_id,
                position=row.position,
                distance=row.distance,
            )
            for row in result
        ]
