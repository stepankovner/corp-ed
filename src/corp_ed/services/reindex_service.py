from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.exceptions import NotFoundError
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import MaterialStatus, Tenant
from corp_ed.repositories.ingest_job_repository import IngestJobRepository
from corp_ed.repositories.material_repository import MaterialRepository
from corp_ed.repositories.tenant_repository import TenantRepository

logger = structlog.get_logger()


@dataclass(frozen=True)
class ReindexReport:
    company_code: str
    materials: int
    queued: int
    """Сколько поставлено в очередь; остальные уже стояли в очереди."""


class ReindexService:
    """Переиндексация всех материалов компании или всех компаний (BH-6).

    Нужна после каждой смены нарезки, предобработки или модели
    эмбеддингов. Сама ничего не считает — ставит задачи в ту же очередь,
    что и загрузка: темп квоты эмбеддингов держит воркер, а замена чанков
    материала атомарна (IngestService.ingest).

    Каждая компания — своя сессия и свой tenant_scope: RLS и хук
    изоляции работают так же, как в запросе пользователя.
    """

    def __init__(self, session_maker: async_sessionmaker[AsyncSession]) -> None:
        self.session_maker = session_maker

    async def reindex(
        self, *, company_code: str | None, dry_run: bool
    ) -> list[ReindexReport]:
        async with self.session_maker() as session:
            repo = TenantRepository(session)
            if company_code is None:
                tenants = await repo.list_all()
            else:
                tenant = await repo.get_by_company_code(company_code)
                if tenant is None:
                    raise NotFoundError(f"Компании с кодом '{company_code}' нет")
                tenants = [tenant]

        return [await self._reindex_tenant(tenant, dry_run) for tenant in tenants]

    async def _reindex_tenant(self, tenant: Tenant, dry_run: bool) -> ReindexReport:
        with tenant_scope(tenant.id):
            async with self.session_maker() as session:
                materials = await MaterialRepository(session).list_all()
                queued = 0
                if not dry_run:
                    jobs = IngestJobRepository(session)
                    for material in materials:
                        if await jobs.enqueue(tenant.id, material.id):
                            material.status = MaterialStatus.PENDING
                            material.status_error = None
                            queued += 1
                    await session.commit()

        logger.info(
            "reindex_queued",
            tenant_id=str(tenant.id),
            materials=len(materials),
            queued=queued,
            dry_run=dry_run,
        )
        return ReindexReport(
            company_code=tenant.company_code,
            materials=len(materials),
            queued=queued,
        )
