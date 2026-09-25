from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import NotFoundError
from corp_ed.domain.gaps import mask_pii
from corp_ed.domain.models import GapCluster, User
from corp_ed.domain.types import GapStatus
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.repositories.gap_repository import GapRepository

SAMPLE_QUESTIONS = 5


@dataclass(frozen=True)
class GapReportItem:
    cluster: GapCluster
    sample_questions: list[str]


class GapService:
    """Отчёт о пробелах для админа компании (BH-23).

    Строит отчёт ночная задача (GapReportService); здесь — чтение и
    статус, который ставит админ.
    """

    def __init__(
        self,
        repository: GapRepository,
        audit: AuditRepository,
        session: AsyncSession,
    ) -> None:
        self.repository = repository
        self.audit = audit
        self.session = session

    async def report(
        self, *, status: GapStatus | None, limit: int
    ) -> list[GapReportItem]:
        clusters = await self.repository.list_for_report(
            status=status.value if status else None, limit=limit
        )
        return await self._with_samples(clusters)

    async def set_status(
        self, actor: User, cluster_id: UUID, status: GapStatus
    ) -> GapReportItem:
        cluster = await self.repository.get_by_id(cluster_id)
        if cluster is None:
            raise NotFoundError("Пробел не найден")
        previous = cluster.status
        cluster.status = status.value
        self.audit.record(
            AuditAction.GAP_STATUS_CHANGED,
            tenant_id=cluster.tenant_id,
            actor_id=actor.id,
            target_type="gap_cluster",
            target_id=cluster.id,
            details={"from": previous, "to": status.value},
        )
        await self.session.commit()
        await self.session.refresh(cluster)
        [item] = await self._with_samples([cluster])
        return item

    async def _with_samples(self, clusters: list[GapCluster]) -> list[GapReportItem]:
        samples = await self.repository.samples(
            [cluster.id for cluster in clusters], per_cluster=SAMPLE_QUESTIONS
        )
        return [
            GapReportItem(
                cluster=cluster,
                # В qa_log вопрос уже замаскирован. Ещё раз — на случай
                # строк, записанных до улучшения mask_pii: отчёт видит
                # админ, а не только система.
                sample_questions=[mask_pii(q) for q in samples.get(cluster.id, [])],
            )
            for cluster in clusters
        ]
