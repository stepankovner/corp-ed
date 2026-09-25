from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import NotFoundError
from corp_ed.domain.models import Chunk, Material, User
from corp_ed.domain.split import split_document
from corp_ed.ingest.preprocess import preprocess
from corp_ed.llm.embedding_gateway import EmbeddingGateway
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
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
        audit: AuditRepository,
        session: AsyncSession,
        chunk_tokens: int,
        overlap_tokens: int,
    ) -> None:
        self.material_repo = material_repo
        self.chunk_repo = chunk_repo
        self.embedding_gateway = embedding_gateway
        self.audit = audit
        self.session = session
        self.chunk_tokens = chunk_tokens
        self.overlap_tokens = overlap_tokens

    async def create(
        self,
        actor: User,
        *,
        title: str,
        content: str,
    ) -> Material:
        material = Material(
            title=title,
            content=content,
        )

        await self.material_repo.create(material)
        self.audit.record(
            AuditAction.MATERIAL_CREATED,
            tenant_id=material.tenant_id,
            actor_id=actor.id,
            target_type="material",
            target_id=material.id,
        )
        await self.session.commit()

        logger.info(
            "material_created",
            material_id=str(material.id),
            content_length=len(content),
        )

        return material

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

        # material.content хранит Markdown до предобработки: если ML
        # поменяет preprocess, переиндексация подхватит новую версию
        # без повторной загрузки файла.
        drafts = split_document(
            preprocess(material.content),
            title=material.title,
            chunk_tokens=self.chunk_tokens,
            overlap_tokens=self.overlap_tokens,
        )
        logger.info(
            "material_split",
            material_id=str(material_id),
            chunks=len(drafts),
        )

        chunks: list[Chunk] = []
        input_tokens = 0

        for draft in drafts:
            # Эмбеддинг — по embed_text, НЕ по llm_text: разметка в
            # векторе — шум, тихая потеря качества поиска (BH-3).
            result = await self.embedding_gateway.embed_document(draft.embed_text)
            input_tokens += result.input_tokens
            chunks.append(
                Chunk(
                    material_id=material.id,
                    position=draft.position,
                    heading_path=draft.heading_path,
                    embed_text=draft.embed_text,
                    content=draft.llm_text,
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
