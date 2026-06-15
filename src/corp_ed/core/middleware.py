import uuid
from collections.abc import Awaitable, Callable
from uuid import UUID

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from corp_ed.core.tenant_context import current_tenant


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Присваивает каждому запросу уникальный request_id и кладёт в лог-контекст."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = str(uuid.uuid4())

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


class TenantMiddleware(BaseHTTPMiddleware):
    """Определяет текущего тенанта запроса и кладёт в контекст для изоляции."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # ВРЕМЕННО: tenant из заголовка. В Phase 7 заменить на извлечение из токена.
        tenant_header = request.headers.get("X-Tenant-ID")
        if tenant_header:
            current_tenant.set(UUID(tenant_header))
        response = await call_next(request)
        return response
