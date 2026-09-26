import hashlib
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import (
    DuplicateMaterialError,
    NotFoundError,
    UnacceptableFileError,
)
from corp_ed.domain.models import Material, MaterialStatus, User
from corp_ed.ingest.extract import ERROR_MESSAGES, ExtractionError, detect_format
from corp_ed.ingest.sandbox import extract_isolated
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.repositories.ingest_job_repository import IngestJobRepository
from corp_ed.repositories.material_repository import MaterialRepository

logger = structlog.get_logger()


class MaterialService:
    """Документы компании со стороны API: создать, показать, поставить
    в очередь на индексацию. Сама индексация — IngestService в воркере."""

    def __init__(
        self,
        material_repo: MaterialRepository,
        job_repo: IngestJobRepository,
        audit: AuditRepository,
        session: AsyncSession,
    ) -> None:
        self.material_repo = material_repo
        self.job_repo = job_repo
        self.audit = audit
        self.session = session

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
        # Задача ставится в той же транзакции: материал без задачи или
        # задача без материала невозможны.
        await self.job_repo.enqueue(material.tenant_id, material.id)
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

    async def list_all(self) -> list[Material]:
        return await self.material_repo.list_all()

    async def get(self, material_id: UUID) -> Material:
        material = await self.material_repo.get_by_id(material_id)
        if material is None:
            raise NotFoundError("Материал с таким id не найден")
        return material

    async def request_ingest(self, material_id: UUID) -> Material:
        """Поставить материал в очередь на переиндексацию.

        Идемпотентно: если активная задача уже есть, вторая не создаётся —
        повторный клик не плодит параллельные пересчёты одного документа.
        """
        material = await self.get(material_id)
        if await self.job_repo.enqueue(material.tenant_id, material.id):
            material.status = MaterialStatus.PENDING
            material.status_error = None
        await self.session.commit()
        return material

    async def upload(
        self, actor: User, *, title: str, filename: str, data: bytes
    ) -> Material:
        """Принять файл: проверить, извлечь текст в песочнице, поставить в очередь.

        Порядок важен: дешёвые проверки (сигнатура, zip-бомба) — в этом
        процессе, разбор — в изолированном, и только потом база. Во время
        разбора (до 90 с) транзакция не открыта и соединение пула не занято.
        Исходный файл не сохраняется — только текст и sha256.
        """
        try:
            detected = detect_format(filename, data)
            markdown = await extract_isolated(detected.format, data)
        except ExtractionError as exc:
            logger.info("upload_rejected", code=exc.code, size=len(data))
            raise UnacceptableFileError(exc.code, ERROR_MESSAGES[exc.code]) from exc

        sha256 = hashlib.sha256(data).hexdigest()
        existing = await self.material_repo.get_by_sha256(sha256)
        if existing is not None:
            raise DuplicateMaterialError(existing.id)

        material = Material(
            title=title,
            content=markdown,
            source_filename=detected.filename,
            source_format=detected.format.value,
            source_sha256=sha256,
            source_size=len(data),
        )
        await self.material_repo.create(material)
        await self.job_repo.enqueue(material.tenant_id, material.id)
        self.audit.record(
            AuditAction.MATERIAL_CREATED,
            tenant_id=material.tenant_id,
            actor_id=actor.id,
            target_type="material",
            target_id=material.id,
            details={
                "format": detected.format.value,
                "size": len(data),
                "sha256": sha256,
            },
        )
        await self.session.commit()
        logger.info(
            "material_uploaded",
            material_id=str(material.id),
            format=detected.format.value,
            size=len(data),
            chars=len(markdown),
        )
        return material

    async def rename(self, actor: User, material_id: UUID, title: str) -> Material:
        """Переименовать и переиндексировать: название — в крошках каждого
        чанка и в эмбеддинге, без пересчёта поиск помнил бы старое."""
        material = await self.get(material_id)
        old_title = material.title
        material.title = title
        if await self.job_repo.enqueue(material.tenant_id, material.id):
            material.status = MaterialStatus.PENDING
            material.status_error = None
        self.audit.record(
            AuditAction.MATERIAL_UPDATED,
            tenant_id=material.tenant_id,
            actor_id=actor.id,
            target_type="material",
            target_id=material.id,
            details={"old_title": old_title, "new_title": title},
        )
        await self.session.commit()
        return material

    async def delete(self, actor: User, material_id: UUID) -> None:
        """Удалить документ вместе с чанками: ответы по нему прекращаются
        сразу, а не после переиндексации."""
        material = await self.get(material_id)
        self.audit.record(
            AuditAction.MATERIAL_DELETED,
            tenant_id=material.tenant_id,
            actor_id=actor.id,
            target_type="material",
            target_id=material.id,
            details={"title": material.title},
        )
        await self.material_repo.delete(material)
        await self.session.commit()
        logger.info("material_deleted", material_id=str(material_id))
