from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import NotFoundError
from corp_ed.domain.models import Chunk, Material, Track
from corp_ed.domain.split import split_into_chunks
from corp_ed.llm.embedding_gateway import EmbeddingGateway
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.repositories.material_repository import MaterialRepository

logger = structlog.get_logger()


class MaterialService:
    """Бизнес-логика работы с материалами."""

    def __init__(
        self,
        material_repo: MaterialRepository,
        chunk_repo: ChunkRepository,
        embedding_gateway: EmbeddingGateway,
        session: AsyncSession,
        chunk_size: int,
        overlap: int,
    ) -> None:
        self.material_repo = material_repo
        self.chunk_repo = chunk_repo
        self.embedding_gateway = embedding_gateway
        self.session = session
        self.chunk_size = chunk_size
        self.overlap = overlap

    async def create(
        self,
        *,
        track: Track,
        title: str,
        content: str,
    ) -> Material:
        material = Material(
            track=track,
            title=title,
            content=content,
        )

        await self.material_repo.create(material)
        await self.session.commit()

        logger.info(
            "material_created",
            material_id=str(material.id),
            content_length=len(content),
        )

        return material

    async def list_all(self) -> list[Material]:
        """Материалы тенанта, новые сверху."""
        return await self.material_repo.list_all()

    async def ingest(self, material_id: UUID) -> int:
        """Пересчитать чанки материала.

        Эмбеддинги считаются до открытия транзакции: это сотни
        миллисекунд на чанк, и держать блокировки всё это время нельзя.
        Пока идёт счёт, в базе лежит прошлая версия чанков и поиск по
        материалу продолжает работать; подмена происходит атомарно
        на коммите.
        """
        material = await self.material_repo.get_by_id(material_id)
        if material is None:
            logger.warning("material_not_found", material_id=str(material_id))
            raise NotFoundError("Материал с таким id не найден")

        text_chunks = split_into_chunks(
            material.content,
            chunk_size=self.chunk_size,
            overlap=self.overlap,
        )
        logger.info(
            "material_split",
            material_id=str(material_id),
            chunks=len(text_chunks),
        )

        chunks: list[Chunk] = []
        input_tokens = 0

        for position, text in enumerate(text_chunks):
            result = await self.embedding_gateway.embed_document(text)
            input_tokens += result.input_tokens
            chunks.append(
                Chunk(
                    material_id=material.id,
                    position=position,
                    content=text,
                    embedding=result.embedding,
                    model=result.model,
                    model_version=result.model_version,
                )
            )

        await self.chunk_repo.delete_by_material(material.id)
        await self.chunk_repo.bulk_create(chunks)
        await self.session.commit()

        logger.info(
            "material_ingested",
            material_id=str(material_id),
            chunks=len(chunks),
            input_tokens=input_tokens,
        )
        return len(chunks)
