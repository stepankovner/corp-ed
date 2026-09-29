"""Метаданные текущего запроса для аудита.

Сервисы не знают про HTTP, но запись аудита должна нести IP и
request_id. Их выставляет RequestIDMiddleware в начале запроса; вне
HTTP (CLI, фоновые задачи) значения пустые, и это видно в записи.
"""

from contextvars import ContextVar

current_request_id: ContextVar[str | None] = ContextVar(
    "current_request_id", default=None
)
current_client_ip: ContextVar[str | None] = ContextVar(
    "current_client_ip", default=None
)
