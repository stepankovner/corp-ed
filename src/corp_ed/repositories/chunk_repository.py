from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import TenantContextMissingError
from corp_ed.core.tenant_context import current_tenant
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
        stmt = delete(Chunk).where(Chunk.material_id == material_id)
        await self.session.execute(stmt)

    async def search(self, embedding: list[float], limit: int = 5) -> list[ChunkMatch]:
        tenant_id = current_tenant.get()
        if tenant_id is None:
            raise TenantContextMissingError("В контексте отсутствует tenant_id")

        distance = Chunk.embedding.cosine_distance(embedding)

        stmt = (
            select(
                Chunk.id,
                Chunk.content,
                Chunk.material_id,
                Chunk.position,
                distance.label("distance"),
            )
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
