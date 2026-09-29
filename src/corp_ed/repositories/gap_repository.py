from collections import defaultdict
from collections.abc import Iterable, Sequence
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.tenant_context import require_tenant
from corp_ed.domain.models import GapCluster, GapClusterQuestion, QaLog


class GapRepository:
    """Кластеры пробелов и их состав (BH-22).

    Сущности GapCluster грузятся ORM-запросами — их фильтрует хук
    изоляции. Состав и выборки вопросов — колоночные, фильтр явный.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, cluster_id: UUID) -> GapCluster | None:
        # select, а не session.get: см. UserRepository.get_by_id.
        result = await self.session.scalars(
            select(GapCluster).where(GapCluster.id == cluster_id)
        )
        return result.first()

    async def list_for_report(
        self, *, status: str | None, limit: int
    ) -> list[GapCluster]:
        stmt = select(GapCluster)
        if status is not None:
            stmt = stmt.where(GapCluster.status == status)
        stmt = stmt.order_by(GapCluster.priority.desc(), GapCluster.id).limit(limit)
        result = await self.session.scalars(stmt)
        return list(result)

    async def list_with_members(self) -> list[tuple[GapCluster, set[UUID]]]:
        clusters = list(await self.session.scalars(select(GapCluster)))
        members: dict[UUID, set[UUID]] = defaultdict(set)
        rows = await self.session.execute(
            select(GapClusterQuestion.cluster_id, GapClusterQuestion.qa_log_id).where(
                GapClusterQuestion.tenant_id == require_tenant()
            )
        )
        for cluster_id, qa_log_id in rows:
            members[cluster_id].add(qa_log_id)
        return [(cluster, members[cluster.id]) for cluster in clusters]

    async def samples(
        self, cluster_ids: Sequence[UUID], per_cluster: int
    ) -> dict[UUID, list[str]]:
        """Последние вопросы каждого кластера — уже после mask_pii."""
        if not cluster_ids:
            return {}
        tenant_id = require_tenant()
        rows = await self.session.execute(
            select(GapClusterQuestion.cluster_id, QaLog.question)
            .join(QaLog, QaLog.id == GapClusterQuestion.qa_log_id)
            .where(
                GapClusterQuestion.tenant_id == tenant_id,
                QaLog.tenant_id == tenant_id,
                GapClusterQuestion.cluster_id.in_(cluster_ids),
            )
            .order_by(QaLog.created_at.desc())
        )
        samples: dict[UUID, list[str]] = defaultdict(list)
        for cluster_id, question in rows:
            if len(samples[cluster_id]) < per_cluster:
                samples[cluster_id].append(question)
        return dict(samples)

    async def add(self, cluster: GapCluster) -> GapCluster:
        self.session.add(cluster)
        await self.session.flush()
        return cluster

    async def replace_members(
        self, cluster_id: UUID, qa_log_ids: Iterable[UUID]
    ) -> None:
        tenant_id = require_tenant()
        await self.session.execute(
            delete(GapClusterQuestion).where(
                GapClusterQuestion.tenant_id == tenant_id,
                GapClusterQuestion.cluster_id == cluster_id,
            )
        )
        self.session.add_all(
            GapClusterQuestion(cluster_id=cluster_id, qa_log_id=qa_log_id)
            for qa_log_id in qa_log_ids
        )
        await self.session.flush()

    async def delete(self, cluster: GapCluster) -> None:
        # Состав удаляет база: ON DELETE CASCADE.
        await self.session.delete(cluster)
        await self.session.flush()
