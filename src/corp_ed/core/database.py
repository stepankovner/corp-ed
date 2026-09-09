from collections.abc import AsyncGenerator
from functools import lru_cache

from sqlalchemy import event
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

from corp_ed.core.config import settings
from corp_ed.core.exceptions import TenantContextMissingError, TenantMismatchError
from corp_ed.core.tenant_context import current_tenant
from corp_ed.domain.mixins import TenantMixin


class Base(DeclarativeBase):
    """Базовый класс для всех ORM-моделей."""


@lru_cache
def get_engine() -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        echo=settings.debug,
    )


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


@event.listens_for(Session, "do_orm_execute")
def _apply_tenant_filter(execute_state: ORMExecuteState) -> None:
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
