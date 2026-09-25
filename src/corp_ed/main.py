import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.middleware.trustedhost import TrustedHostMiddleware

from corp_ed.api.v1.endpoints import audit, auth, faq, materials, users
from corp_ed.core.config import LLMSettings, get_http_settings
from corp_ed.core.database import get_engine
from corp_ed.core.exception_handlers import (
    conflict_error_handler,
    domain_fallback_handler,
    internal_error_handler,
    invalid_credentials_handler,
    llm_error_handler,
    not_authenticated_handler,
    not_found_error_handler,
    permission_error_handler,
    rate_limited_handler,
    service_unavailable_handler,
    validation_error_handler,
    weak_password_handler,
)
from corp_ed.core.exceptions import (
    ConflictError,
    DomainError,
    InvalidCredentialsError,
    NotAuthenticatedError,
    NotFoundError,
    PermissionError,
    ServiceUnavailableError,
    TenantContextMissingError,
    TenantMismatchError,
    WeakPasswordError,
)
from corp_ed.core.logging import configure_logging
from corp_ed.core.middleware import (
    BodySizeLimitMiddleware,
    RequestIDMiddleware,
    SecurityHeadersMiddleware,
)
from corp_ed.core.rate_limit import (
    InMemoryRateLimiter,
    RateLimitedError,
    RateLimiter,
    RedisRateLimiter,
)
from corp_ed.llm.errors import LLMError

logger = structlog.get_logger()

# Пути, где тело — файл, а не JSON: у них свой лимит размера.
UPLOAD_PATH_SUFFIXES = ("/materials/upload",)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    # Проверка базы на старте (RISKS №6): битая строка подключения
    # должна ронять контейнер, а не превращаться в 500 на первом запросе.
    async with get_engine().connect() as connection:
        await connection.execute(text("SELECT 1"))
        await _check_database_role(connection)
    redis: Redis | None = None
    limiter: RateLimiter
    if http_settings.redis_url is not None:
        redis = Redis.from_url(http_settings.redis_url.get_secret_value())
        await redis.ping()
        limiter = RedisRateLimiter(redis)
    else:
        logger.warning("rate_limiter_in_memory")
        limiter = InMemoryRateLimiter()
    app.state.rate_limiter = limiter

    # Семафор генерации — один на процесс: адаптер создаётся на запрос.
    # Размер читается лениво: без YC-ключей (тесты, alembic) он не нужен.
    app.state.llm_semaphore = asyncio.Semaphore(_llm_concurrency())

    app.state.http_client = httpx.AsyncClient()
    try:
        yield
    finally:
        await app.state.http_client.aclose()
        if redis is not None:
            await redis.aclose()


def _llm_concurrency() -> int:
    try:
        return LLMSettings().llm_max_concurrency  # type: ignore[call-arg]
    except ValidationError:
        # Нет ключей провайдера — ответы всё равно не заработают, а
        # запуск ради остальных ручек (вход, пользователи) нужен.
        logger.warning("llm_settings_missing")
        return 1


async def _check_database_role(connection: AsyncConnection) -> None:
    """RLS не действует для SUPERUSER и BYPASSRLS — молча, без ошибок.

    Приложение под такой ролью работало бы «как обычно», но второй
    рубеж изоляции был бы выключен. В production это ошибка старта,
    в разработке — предупреждение (локальный postgres из compose
    по умолчанию даёт суперпользователя).
    """
    row = (
        await connection.execute(
            text(
                "SELECT rolsuper, rolbypassrls FROM pg_roles "
                "WHERE rolname = current_user"
            )
        )
    ).one()
    if row.rolsuper or row.rolbypassrls:
        if http_settings.is_production:
            raise RuntimeError(
                "Database role bypasses row-level security; "
                "use a role without SUPERUSER and BYPASSRLS (see docs/DEPLOY.md)"
            )
        logger.warning("database_role_bypasses_rls")


http_settings = get_http_settings()

# В production схема API не публикуется: /docs и /openapi.json — готовая
# карта ручек и полей для атакующего. Фронтенд получает контракт из
# репозитория, а не с боевого сервера.
_docs = not http_settings.is_production

app = FastAPI(
    title="corp-ed",
    description="Kronto — ИИ-ассистент по внутренним документам компании",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if _docs else None,
    redoc_url="/redoc" if _docs else None,
    openapi_url="/openapi.json" if _docs else None,
)

app.include_router(auth.router, prefix="/api/v1")
app.include_router(users.router, prefix="/api/v1")
app.include_router(materials.router, prefix="/api/v1")
app.include_router(faq.router, prefix="/api/v1")
app.include_router(audit.router, prefix="/api/v1")


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
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(TenantContextMissingError, internal_error_handler)
app.add_exception_handler(TenantMismatchError, internal_error_handler)
app.add_exception_handler(RateLimitedError, rate_limited_handler)
app.add_exception_handler(ServiceUnavailableError, service_unavailable_handler)

# Выполняются в порядке, обратном добавлению. Снаружи внутрь:
#   CORS → заголовки безопасности → request_id и ловушка 500 →
#   проверка Host → лимит тела → приложение.
# Лимит тела — самый внутренний: его 413 проходит через все внешние
# слои и получает заголовки и request_id, как любой другой ответ.
app.add_middleware(
    BodySizeLimitMiddleware,
    max_body_bytes=http_settings.max_body_bytes,
    max_upload_bytes=http_settings.max_upload_bytes,
    upload_paths=UPLOAD_PATH_SUFFIXES,
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=http_settings.hosts)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(SecurityHeadersMiddleware, hsts=http_settings.is_production)

# CORS снаружи всех: иначе ответы с ошибками уходили бы в браузер без
# CORS-заголовков, и фронтенд видел бы вместо 403 непрозрачную ошибку
# сети. allow_credentials выключен: токен ходит в заголовке, куки нет —
# разрешить браузеру слать учётные данные с чужой страницы незачем.
if http_settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=http_settings.cors_origins,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["X-Request-ID"],
        max_age=600,
    )
