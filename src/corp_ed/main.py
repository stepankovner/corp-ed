from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from corp_ed.api.v1.endpoints import auth, faq, materials, users
from corp_ed.core.exception_handlers import (
    conflict_error_handler,
    domain_fallback_handler,
    invalid_credentials_handler,
    llm_error_handler,
    not_authenticated_handler,
    not_found_error_handler,
    permission_error_handler,
    weak_password_handler,
)
from corp_ed.core.exceptions import (
    ConflictError,
    DomainError,
    InvalidCredentialsError,
    NotAuthenticatedError,
    NotFoundError,
    PermissionError,
    WeakPasswordError,
)
from corp_ed.core.logging import configure_logging
from corp_ed.core.middleware import RequestIDMiddleware
from corp_ed.llm.errors import LLMError


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    app.state.http_client = httpx.AsyncClient()
    try:
        yield
    finally:
        await app.state.http_client.aclose()


app = FastAPI(
    title="corp-ed",
    description="Kronto — ИИ-ассистент по внутренним документам компании",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(auth.router, prefix="/api/v1")
app.include_router(users.router, prefix="/api/v1")
app.include_router(materials.router, prefix="/api/v1")
app.include_router(faq.router, prefix="/api/v1")


@app.get("/")
def read_root() -> dict[str, str]:
    return {"status": "ok"}


app.add_exception_handler(DomainError, domain_fallback_handler)
app.add_exception_handler(ConflictError, conflict_error_handler)
app.add_exception_handler(NotFoundError, not_found_error_handler)
app.add_exception_handler(PermissionError, permission_error_handler)
app.add_exception_handler(InvalidCredentialsError, invalid_credentials_handler)
app.add_exception_handler(NotAuthenticatedError, not_authenticated_handler)
app.add_exception_handler(LLMError, llm_error_handler)
app.add_exception_handler(WeakPasswordError, weak_password_handler)

# выполняются в порядке, обратном добавлению
app.add_middleware(RequestIDMiddleware)
