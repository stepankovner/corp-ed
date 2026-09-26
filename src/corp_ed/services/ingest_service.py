from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import NotFoundError
from corp_ed.domain.models import Chunk, Material, MaterialStatus
from corp_ed.domain.split import split_document
from corp_ed.ingest.preprocess import preprocess
from corp_ed.llm.embedding_gateway import EmbeddingGateway
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.repositories.material_repository import MaterialRepository

logger = structlog.get_logger()


class IngestService:
    """Нарезка материала, эмбеддинги и замена чанков. Вызывает воркер.

    Тенант — из контекста: воркер выставляет tenant_scope задачи до
    вызова, и все выборки проходят и хук изоляции, и RLS.
    """

    def __init__(
        self,
        material_repo: MaterialRepository,
        chunk_repo: ChunkRepository,
        embedding_gateway: EmbeddingGateway,
        session: AsyncSession,
        chunk_tokens: int,
        overlap_tokens: int,
    ) -> None:
        self.material_repo = material_repo
        self.chunk_repo = chunk_repo
        self.embedding_gateway = embedding_gateway
        self.session = session
        self.chunk_tokens = chunk_tokens
        self.overlap_tokens = overlap_tokens

    async def ingest(self, material_id: UUID) -> int:
        """Пересчитать чанки материала.

        Статус PROCESSING фиксируется отдельным коммитом до эмбеддингов:
        админ видит, что материал в работе, а транзакция не висит открытой
        все секунды, пока считаются векторы. Пока идёт счёт, в базе лежит
        прошлая версия чанков и поиск по материалу продолжает работать;
        подмена происходит атомарно на коммите вместе со статусом READY.
        """
        material = await self._get(material_id)
        material.status = MaterialStatus.PROCESSING
        material.status_error = None
        # Снимок полей до коммита: expire_on_commit=False, но читать ORM-
        # объект между транзакциями — лишняя связь с состоянием сессии.
        title, content = material.title, material.content
        await self.session.commit()

        # material.content хранит Markdown до предобработки: если ML
        # поменяет preprocess, переиндексация подхватит новую версию
        # без повторной загрузки файла.
        drafts = split_document(
            preprocess(content),
            title=title,
            chunk_tokens=self.chunk_tokens,
            overlap_tokens=self.overlap_tokens,
        )
        logger.info("material_split", material_id=str(material_id), chunks=len(drafts))

        chunks: list[Chunk] = []
        input_tokens = 0

        for draft in drafts:
            # Эмбеддинг — по embed_text, НЕ по llm_text: разметка в
            # векторе — шум, тихая потеря качества поиска (BH-3).
            result = await self.embedding_gateway.embed_document(draft.embed_text)
            input_tokens += result.input_tokens
            chunks.append(
                Chunk(
                    material_id=material_id,
                    position=draft.position,
                    heading_path=draft.heading_path,
                    embed_text=draft.embed_text,
                    content=draft.llm_text,
                    embedding=result.embedding,
                    model=result.model,
                    model_version=result.model_version,
                )
            )

        material = await self._get(material_id)
        await self.chunk_repo.delete_by_material(material_id)
        await self.chunk_repo.bulk_create(chunks)
        material.status = MaterialStatus.READY
        material.indexed_at = datetime.now(UTC)
        await self.session.commit()

        logger.info(
            "material_ingested",
            material_id=str(material_id),
            chunks=len(chunks),
            input_tokens=input_tokens,
        )
        return len(chunks)

    async def mark(
        self, material_id: UUID, status: MaterialStatus, error: str | None
    ) -> None:
        """Выставить статус после неудачи (повтор позже или окончательно)."""
        await self.session.rollback()
        material = await self.material_repo.get_by_id(material_id)
        if material is None:
            return
        material.status = status
        material.status_error = error
        await self.session.commit()

    async def _get(self, material_id: UUID) -> Material:
        material = await self.material_repo.get_by_id(material_id)
        if material is None:
            logger.warning("material_not_found", material_id=str(material_id))
            raise NotFoundError("Материал с таким id не найден")
        return material
