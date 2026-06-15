from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
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

from lms.core.config import settings
from lms.core.exceptions import TenantContextMissingError
from lms.core.tenant_context import current_tenant
from lms.domain.mixins import TenantMixin


class Base(DeclarativeBase):
    """Базовый класс для всех ORM-моделей."""


engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
)

async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncGenerator[AsyncSession]:
    """FastAPI-зависимость: выдаёт сессию БД на время запроса."""
    async with async_session_maker() as session:
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
        return  # запрос только по нетенантным моделям (User) — не трогаем

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
