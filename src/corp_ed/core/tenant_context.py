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
