from contextvars import ContextVar
from uuid import UUID

current_tenant: ContextVar[UUID | None] = ContextVar("current_tenant", default=None)
