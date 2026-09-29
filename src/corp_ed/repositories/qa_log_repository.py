from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import Row, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.tenant_context import require_tenant
from corp_ed.domain.models import QaLog

# id, best_vector_distance, best_fulltext_score, answer_given, feedback, miss_kind
SignalRow = Row[tuple[UUID, float | None, float | None, bool, int | None, str | None]]
# id, user_id, question, question_embedding, created_at
CandidateRow = Row[tuple[UUID, UUID | None, str, list[float], datetime]]


class QaLogRepository:
    """Журнал вопросов и ответов (тенант-таблица под RLS)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, entry: QaLog) -> QaLog:
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def get_by_id(self, entry_id: UUID) -> QaLog | None:
        result = await self.session.scalars(select(QaLog).where(QaLog.id == entry_id))
        return result.first()

    async def credits_since(self, since: datetime) -> int:
        """Кредиты текущей компании с начала периода.

        Журнал ответов и есть книга расхода: отдельный счётчик разошёлся
        бы с ним при первом сбое между двумя записями.
        """
        result = await self.session.scalar(
            select(func.coalesce(func.sum(QaLog.credits), 0)).where(
                QaLog.tenant_id == require_tenant(), QaLog.created_at >= since
            )
        )
        return int(result or 0)

    async def delete_older_than(self, cutoff: datetime) -> int:
        """Удалить записи старше срока хранения в текущем тенанте.

        Bulk DELETE идёт мимо ORM-хуков — фильтр по тенанту явный (и RLS
        в базе как второй рубеж).
        """
        result = await self.session.execute(
            delete(QaLog).where(
                QaLog.tenant_id == require_tenant(), QaLog.created_at < cutoff
            )
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    # --- отчёт о пробелах (BH-21) -------------------------------------------
    # Все выборки — колоночные и массовые: хук изоляции их не видит, фильтр
    # по тенанту явный в каждой (и RLS в базе).

    async def signals_since(self, since: datetime) -> Sequence[SignalRow]:
        """Сигналы classify_miss за окно — без векторов: их не нужно
        грузить для всех строк, только для кандидатов в кластеры."""
        result = await self.session.execute(
            select(
                QaLog.id,
                QaLog.best_vector_distance,
                QaLog.best_fulltext_score,
                QaLog.answer_given,
                QaLog.feedback,
                QaLog.miss_kind,
            ).where(QaLog.tenant_id == require_tenant(), QaLog.created_at >= since)
        )
        return result.all()

    async def set_miss_kind(self, kind: str, ids: Sequence[UUID]) -> None:
        if not ids:
            return
        await self.session.execute(
            update(QaLog)
            .where(QaLog.tenant_id == require_tenant(), QaLog.id.in_(ids))
            .values(miss_kind=kind)
        )

    async def newest_embedding_model(self, since: datetime) -> str | None:
        model: str | None = await self.session.scalar(
            select(QaLog.embedding_model)
            .where(QaLog.tenant_id == require_tenant(), QaLog.created_at >= since)
            .order_by(QaLog.created_at.desc())
            .limit(1)
        )
        return model

    async def gap_candidates(
        self,
        *,
        since: datetime,
        kinds: Sequence[str],
        embedding_model: str,
        limit: int,
    ) -> Sequence[CandidateRow]:
        """Вопросы для кластеризации: новые первыми, одной модели
        эмбеддингов — векторы разных моделей несравнимы (BH-20)."""
        result = await self.session.execute(
            select(
                QaLog.id,
                QaLog.user_id,
                QaLog.question,
                QaLog.question_embedding,
                QaLog.created_at,
            )
            .where(
                QaLog.tenant_id == require_tenant(),
                QaLog.created_at >= since,
                QaLog.miss_kind.in_(kinds),
                QaLog.embedding_model == embedding_model,
            )
            .order_by(QaLog.created_at.desc())
            .limit(limit)
        )
        return result.all()
