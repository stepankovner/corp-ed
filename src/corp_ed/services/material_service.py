from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import NotFoundError
from corp_ed.domain.models import Material, MaterialStatus, User
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
