from collections.abc import AsyncGenerator
from functools import lru_cache

from sqlalchemy import Connection, event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    ORMExecuteState,
    Session,
    with_loader_criteria,
)
from sqlalchemy.orm.attributes import get_history

from corp_ed.core.config import get_settings
from corp_ed.core.db_policies import TENANT_SETTING
from corp_ed.core.exceptions import TenantContextMissingError, TenantMismatchError
from corp_ed.core.tenant_context import current_tenant
from corp_ed.domain.mixins import TenantMixin


class Base(DeclarativeBase):
    """Базовый класс для всех ORM-моделей."""


@lru_cache
def get_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(settings.database_url, echo=settings.debug)


@lru_cache
def get_session_maker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        get_engine(),
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def get_session() -> AsyncGenerator[AsyncSession]:
    """FastAPI-зависимость: выдаёт сессию БД на время запроса."""
    session_maker = get_session_maker()
    async with session_maker() as session:
        yield session


_RLS_TENANT_KEY = "rls_tenant"
_SET_TENANT = text(f"SELECT set_config('{TENANT_SETTING}', :tenant, true)")


def _sync_rls_tenant(session: Session, connection: Connection) -> None:
    """Передать тенанта из контекста в параметр сессии PostgreSQL для RLS.

    set_config(..., true) действует до конца транзакции, поэтому значение
    ставится в начале каждой транзакции (after_begin) и заново, если
    контекст сменился посреди неё (tenant_scope при входе, в CLI).
    Последнее выставленное значение кешируется в session.info, чтобы не
    ходить в базу на каждый запрос. Вызов идёт через Connection, а не
    Session: иначе он сам бы запустил do_orm_execute.
    """
    tenant_id = current_tenant.get()
    value = str(tenant_id) if tenant_id is not None else ""
    if session.info.get(_RLS_TENANT_KEY) == value:
        return
    connection.execute(_SET_TENANT, {"tenant": value})
    session.info[_RLS_TENANT_KEY] = value


@event.listens_for(Session, "after_begin")
def _bind_rls_tenant_on_begin(
    session: Session, transaction: object, connection: Connection
) -> None:
    # Новая транзакция — параметр в базе сброшен, кеш тоже.
    session.info.pop(_RLS_TENANT_KEY, None)
    _sync_rls_tenant(session, connection)


@event.listens_for(Session, "do_orm_execute")
def _apply_tenant_filter(execute_state: ORMExecuteState) -> None:
    # Любой запрос через Session, включая text() и bulk DELETE/UPDATE:
    # сначала база должна знать актуального тенанта.
    _sync_rls_tenant(execute_state.session, execute_state.session.connection())

    if not execute_state.is_select:
        return

    # участвует ли в запросе хоть одна тенант-скоупная сущность?
    involves_tenant_model = any(
        issubclass(m.class_, TenantMixin) for m in execute_state.all_mappers
    )
    if not involves_tenant_model:
        return

    # дошли сюда => в запросе есть тенант-модель => тенант ОБЯЗАТЕЛЕН
    tenant_id = current_tenant.get()
    if tenant_id is None:
        raise TenantContextMissingError()

    execute_state.statement = execute_state.statement.options(
        with_loader_criteria(
            TenantMixin,
            lambda cls: cls.tenant_id == tenant_id,
            include_aliases=True,
        )
    )


@event.listens_for(Session, "before_flush")
def _check_tenant_on_write(
    session: Session, flush_context: object, instances: object
) -> None:
    # INSERT/UPDATE при flush идут мимо do_orm_execute — синхронизируем тут.
    _sync_rls_tenant(session, session.connection())
    tenant_id = current_tenant.get()

    for obj in session.new:
        if not isinstance(obj, TenantMixin):
            continue

        if obj.tenant_id is None:
            if tenant_id is None:
                raise TenantContextMissingError()
            obj.tenant_id = tenant_id
        elif tenant_id is not None and obj.tenant_id != tenant_id:
            raise TenantMismatchError("Попытка записи с чужим tenant_id")

    for obj in session.dirty:
        if not isinstance(obj, TenantMixin):
            continue

        history = get_history(obj, "tenant_id")

        if history.has_changes():
            raise TenantMismatchError(
                "Попытка изменить tenant_id у существующей записи"
            )

    for obj in session.deleted:
        if not isinstance(obj, TenantMixin):
            continue

        if tenant_id is None:
            raise TenantContextMissingError()

        if obj.tenant_id != tenant_id:
            raise TenantMismatchError("Попытка удалить запись чужого тенанта")
