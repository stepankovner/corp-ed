from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.tenant_context import require_tenant
from corp_ed.domain.models import QaLog


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
