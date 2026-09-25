from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from corp_ed.core.tenant_context import require_tenant
from corp_ed.domain.models import Connector, ConnectorSyncRun, ConnectorUserGrant
from corp_ed.domain.types import ConnectorMode, ConnectorStatus, GrantStatus


class ConnectorRepository:
    """Подключения компании (тенант-таблица под RLS)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self) -> list[Connector]:
        result = await self.session.scalars(
            select(Connector).order_by(Connector.created_at)
        )
        return list(result)

    async def get_by_id(self, connector_id: UUID) -> Connector | None:
        # select, а не session.get: см. UserRepository.get_by_id.
        result = await self.session.scalars(
            select(Connector).where(Connector.id == connector_id)
        )
        return result.first()

    async def get_with_credentials(self, connector_id: UUID) -> Connector | None:
        """С шифротекстом учётных данных — только воркеру и проверке."""
        result = await self.session.scalars(
            select(Connector)
            .options(undefer(Connector.credentials))
            .where(Connector.id == connector_id)
        )
        return result.first()

    async def count(self) -> int:
        # Колоночный select: хук изоляции его не видит, фильтр явный.
        result = await self.session.scalar(
            select(func.count())
            .select_from(Connector)
            .where(Connector.tenant_id == require_tenant())
        )
        return int(result or 0)

    async def list_due(self, now: datetime) -> list[Connector]:
        """Активные подключения, чей интервал истёк.

        Режим organization без учётных данных не ставится: запуск сразу
        остановил бы коннектор с ошибкой, а админ его ещё настраивает.
        """
        interval = Connector.sync_interval_minutes * timedelta(minutes=1)
        result = await self.session.scalars(
            select(Connector).where(
                Connector.tenant_id == require_tenant(),
                Connector.status == ConnectorStatus.ACTIVE.value,
                or_(
                    Connector.mode == ConnectorMode.PER_USER.value,
                    Connector.credentials_set_at.is_not(None),
                ),
                or_(
                    Connector.last_sync_at.is_(None),
                    Connector.last_sync_at + interval <= now,
                ),
            )
        )
        return list(result)

    async def create(self, connector: Connector) -> Connector:
        self.session.add(connector)
        await self.session.flush()
        await self.session.refresh(connector)
        return connector

    async def delete(self, connector: Connector) -> None:
        # Документы, гранты, запуски и задачи — каскадом в базе.
        await self.session.delete(connector)
        await self.session.flush()


class SyncRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, run: ConnectorSyncRun) -> ConnectorSyncRun:
        self.session.add(run)
        await self.session.flush()
        return run

    async def list_for_connector(
        self, connector_id: UUID, *, limit: int
    ) -> list[ConnectorSyncRun]:
        result = await self.session.scalars(
            select(ConnectorSyncRun)
            .where(ConnectorSyncRun.connector_id == connector_id)
            .order_by(ConnectorSyncRun.started_at.desc())
            .limit(limit)
        )
        return list(result)

    async def delete_older_than(self, cutoff: datetime) -> int:
        # Bulk DELETE идёт мимо ORM-хуков — фильтр по тенанту явный.
        result = await self.session.execute(
            delete(ConnectorSyncRun).where(
                ConnectorSyncRun.tenant_id == require_tenant(),
                ConnectorSyncRun.started_at < cutoff,
            )
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


class GrantRepository:
    """Авторизации сотрудников в коннекторах режима per_user."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, grant_id: UUID) -> ConnectorUserGrant | None:
        result = await self.session.scalars(
            select(ConnectorUserGrant).where(ConnectorUserGrant.id == grant_id)
        )
        return result.first()

    async def get(self, connector_id: UUID, user_id: UUID) -> ConnectorUserGrant | None:
        result = await self.session.scalars(
            select(ConnectorUserGrant).where(
                ConnectorUserGrant.connector_id == connector_id,
                ConnectorUserGrant.user_id == user_id,
            )
        )
        return result.first()

    async def list_active_with_credentials(
        self, connector_id: UUID
    ) -> list[ConnectorUserGrant]:
        result = await self.session.scalars(
            select(ConnectorUserGrant)
            .options(undefer(ConnectorUserGrant.credentials))
            .where(
                ConnectorUserGrant.connector_id == connector_id,
                ConnectorUserGrant.status == GrantStatus.ACTIVE.value,
            )
            .order_by(ConnectorUserGrant.created_at)
        )
        return list(result)

    async def list_for_user(self, user_id: UUID) -> list[ConnectorUserGrant]:
        result = await self.session.scalars(
            select(ConnectorUserGrant).where(ConnectorUserGrant.user_id == user_id)
        )
        return list(result)

    async def count_active(self, connector_id: UUID) -> int:
        result = await self.session.scalar(
            select(func.count())
            .select_from(ConnectorUserGrant)
            .where(
                ConnectorUserGrant.tenant_id == require_tenant(),
                ConnectorUserGrant.connector_id == connector_id,
                ConnectorUserGrant.status == GrantStatus.ACTIVE.value,
            )
        )
        return int(result or 0)

    async def add(self, grant: ConnectorUserGrant) -> ConnectorUserGrant:
        self.session.add(grant)
        await self.session.flush()
        return grant

    async def delete(self, grant: ConnectorUserGrant) -> None:
        await self.session.delete(grant)
        await self.session.flush()
