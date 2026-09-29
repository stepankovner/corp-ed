from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID

from corp_ed.core.exceptions import TenantContextMissingError

current_tenant: ContextVar[UUID | None] = ContextVar("current_tenant", default=None)


def require_tenant() -> UUID:
    """Вернуть тенанта из контекста или упасть.

    Нужна там, где ORM-хуки не работают: bulk-операции и колоночные
    select идут мимо них, и тенанта приходится подставлять руками.
    """
    tenant_id = current_tenant.get()
    if tenant_id is None:
        raise TenantContextMissingError("В контексте отсутствует tenant_id")
    return tenant_id


@contextmanager
def tenant_scope(tenant_id: UUID) -> Iterator[UUID]:
    """Выставить тенанта на время блока и вернуть прежнее значение после.

    Для кода, где тенант становится известен не из подписанного токена:
    логин (тенант по company_code), обновление токена (тенант из записи
    refresh-токена), CLI, фоновые задачи. Без сброса значение утекало бы
    дальше по коду той же корутины — так было в AuthService.login
    (RISKS.md, пункт 5).
    """
    token = current_tenant.set(tenant_id)
    try:
        yield tenant_id
    finally:
        current_tenant.reset(token)
