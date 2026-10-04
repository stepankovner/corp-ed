import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.middleware.trustedhost import TrustedHostMiddleware

from corp_ed.api.v1.endpoints import (
    account,
    analytics,
    attachments,
    audit,
    auth,
    avatars,
    chat,
    company,
    connectors,
    departments,
    faq,
    folders,
    gaps,
    glossary,
    invites,
    leads,
    logos,
    materials,
    notifications,
    people,
    sources,
    staff,
    suggestions,
    support,
    usage,
    users,
)
from corp_ed.core.config import (
    LLMSettings,
    get_connector_settings,
    get_http_settings,
    get_team_notify_settings,
)
from corp_ed.core.database import get_engine
from corp_ed.core.dialogue_store import InMemoryDialogueStore, RedisDialogueStore
from corp_ed.core.exception_handlers import (
    conflict_error_handler,
    connector_limit_handler,
    credits_exhausted_handler,
    domain_fallback_handler,
    duplicate_material_handler,
    internal_error_handler,
    invalid_connector_config_handler,
    invalid_credentials_handler,
    llm_error_handler,
    not_authenticated_handler,
    not_found_error_handler,
    permission_error_handler,
    rate_limited_handler,
    service_unavailable_handler,
    unacceptable_file_handler,
    validation_error_handler,
    weak_password_handler,
)
from corp_ed.core.exceptions import (
    ConflictError,
    ConnectorLimitError,
    ConnectorNotInTariffError,
    CreditsExhaustedError,
    DomainError,
    DuplicateMaterialError,
    InvalidConnectorConfigError,
    InvalidCredentialsError,
    InvalidLeadError,
    InviteEmailDomainError,
    NotAuthenticatedError,
    NotFoundError,
    PermissionError,
    ServiceUnavailableError,
    TariffConnectorLimitError,
    TenantContextMissingError,
    TenantMismatchError,
    UnacceptableFileError,
    WeakPasswordError,
)
from corp_ed.core.logging import configure_logging
from corp_ed.core.metrics import MetricsMiddleware, metrics_endpoint
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
from corp_ed.core.readiness import readiness_failures
from corp_ed.llm.errors import LLMError
from corp_ed.llm.throttle import InMemoryThrottle, RedisThrottle
from corp_ed.services.chat_generation import (
    ChatRunner,
    InMemoryStopSignals,
    RedisStopSignals,
)
from corp_ed.services.team_notify import build_team_notifier
from corp_ed.services.team_notify import drain as drain_team_notifier

logger = structlog.get_logger()

# Пути, где тело — файл, а не JSON: у них свой лимит размера.
UPLOAD_PATH_SUFFIXES = (
    "/materials/upload",
    "/attachments",
    "/account/avatar",
    "/company/logo",
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    # Проверка базы на старте (RISKS №6): битая строка подключения
    # должна ронять контейнер, а не превращаться в 500 на первом запросе.
    async with get_engine().connect() as connection:
        await connection.execute(text("SELECT 1"))
        await _check_database_role(connection)
    # В production без ключа шифрования учётных данных источников не
    # стартуем (ConnectorSettings): лучше упасть здесь, чем на первом
    # подключении клиента.
    if not get_connector_settings().keys:
        logger.warning("connector_secrets_key_missing")
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
    # Реплики диалогов (BH-28) — там же, где лимиты: в бою Redis без
    # записи на диск, в разработке — память процесса.
    app.state.redis = redis
    app.state.dialogue_store = (
        RedisDialogueStore(redis) if redis is not None else InMemoryDialogueStore()
    )

    # Семафор генерации — один на процесс: адаптер создаётся на запрос.
    # Размер читается лениво: без YC-ключей (тесты, alembic) он не нужен.
    concurrency, query_rps, ingest_rps = _llm_limits()
    app.state.llm_semaphore = asyncio.Semaphore(concurrency)
    # Темп эмбеддингов вопросов — общий с воркером через Redis (квота
    # каталога одна). Сотрудник ждёт слота не дольше нескольких секунд.
    app.state.embedding_query_throttle = (
        RedisThrottle(redis, "embedding-query", query_rps, max_wait=QUERY_MAX_WAIT)
        if redis is not None
        else InMemoryThrottle(query_rps, max_wait=QUERY_MAX_WAIT)
    )
    # Вложения к вопросу (ТЗ §6) считаются в доле ингеста — общей с
    # воркером: файл сотрудника не отнимает квоту у вопросов коллег.
    app.state.embedding_ingest_throttle = (
        RedisThrottle(
            redis, "embedding-ingest", ingest_rps, max_wait=ATTACHMENT_MAX_WAIT
        )
        if redis is not None
        else InMemoryThrottle(ingest_rps, max_wait=ATTACHMENT_MAX_WAIT)
    )
    # Чат (ТЗ §6): ответы пишутся фоновыми задачами процесса, «Остановить»
    # — флаг в Redis, общий для процессов API.
    app.state.chat_runner = ChatRunner()
    app.state.chat_stop_signals = (
        RedisStopSignals(redis) if redis is not None else InMemoryStopSignals()
    )

    app.state.http_client = httpx.AsyncClient()
    app.state.team_notifier = build_team_notifier(
        app.state.http_client, get_team_notify_settings()
    )
    try:
        yield
    finally:
        # Выкатка: начатые ответы дописываются, пока есть время.
        await app.state.chat_runner.shutdown()
        await drain_team_notifier(app.state.team_notifier)
        await app.state.http_client.aclose()
        if redis is not None:
            await redis.aclose()


QUERY_MAX_WAIT = 5.0
ATTACHMENT_MAX_WAIT = 30.0


def _llm_limits() -> tuple[int, float, float]:
    try:
        settings = LLMSettings()
    except ValidationError:
        # Нет ключей провайдера — ответы всё равно не заработают, а
        # запуск ради остальных ручек (вход, пользователи) нужен.
        logger.warning("llm_settings_missing")
        return 1, 1.0, 1.0
    return (
        settings.llm_max_concurrency,
        settings.embedding_query_rps,
        settings.embedding_ingest_rps,
    )


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
app.include_router(account.router, prefix="/api/v1")
app.include_router(users.router, prefix="/api/v1")
app.include_router(people.router, prefix="/api/v1")
app.include_router(departments.router, prefix="/api/v1")
app.include_router(avatars.router, prefix="/api/v1")
app.include_router(invites.router, prefix="/api/v1")
app.include_router(leads.router, prefix="/api/v1")
app.include_router(materials.router, prefix="/api/v1")
app.include_router(faq.router, prefix="/api/v1")
app.include_router(chat.router, prefix="/api/v1")
app.include_router(attachments.router, prefix="/api/v1")
app.include_router(suggestions.router, prefix="/api/v1")
app.include_router(company.router, prefix="/api/v1")
app.include_router(logos.router, prefix="/api/v1")
app.include_router(analytics.router, prefix="/api/v1")
app.include_router(folders.router, prefix="/api/v1")
app.include_router(sources.router, prefix="/api/v1")
app.include_router(staff.router, prefix="/api/v1")
app.include_router(notifications.router, prefix="/api/v1")
app.include_router(support.router, prefix="/api/v1")
app.include_router(audit.router, prefix="/api/v1")
app.include_router(usage.router, prefix="/api/v1")
app.include_router(glossary.router, prefix="/api/v1")
app.include_router(gaps.router, prefix="/api/v1")
app.include_router(connectors.router, prefix="/api/v1")


@app.get("/")
def read_root() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health", include_in_schema=False)
def health() -> dict[str, str]:
    """Проверка живости для Docker HEALTHCHECK и балансировщика.

    Без базы и Redis намеренно: их сбой — не повод перезапускать
    контейнер приложения. Проходит через TrustedHost, поэтому адрес,
    с которого идёт проверка (127.0.0.1), должен быть в ALLOWED_HOSTS.
    """
    return {"status": "ok"}


@app.get("/health/ready", include_in_schema=False)
async def health_ready(request: Request) -> JSONResponse:
    """Готов ли сервис отвечать: база, Redis, пульс воркера (П-9).

    Её проверяет внешний чекер (Ping-Admin) и blackbox в мониторинге:
    /health живёт, пока жив процесс, а сотруднику нужен весь путь.
    Наружу — только имена упавших частей, без деталей ошибок.
    """
    failed = await readiness_failures(request.app.state)
    if failed:
        return JSONResponse({"status": "fail", "failed": failed}, status_code=503)
    return JSONResponse({"status": "ok"})


app.add_route("/metrics", metrics_endpoint, include_in_schema=False)


app.add_exception_handler(DomainError, domain_fallback_handler)
app.add_exception_handler(ConflictError, conflict_error_handler)
app.add_exception_handler(NotFoundError, not_found_error_handler)
app.add_exception_handler(PermissionError, permission_error_handler)
app.add_exception_handler(InvalidCredentialsError, invalid_credentials_handler)
app.add_exception_handler(NotAuthenticatedError, not_authenticated_handler)
app.add_exception_handler(LLMError, llm_error_handler)
app.add_exception_handler(WeakPasswordError, weak_password_handler)
app.add_exception_handler(InviteEmailDomainError, weak_password_handler)
app.add_exception_handler(InvalidLeadError, weak_password_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(TenantContextMissingError, internal_error_handler)
app.add_exception_handler(TenantMismatchError, internal_error_handler)
app.add_exception_handler(RateLimitedError, rate_limited_handler)
app.add_exception_handler(ServiceUnavailableError, service_unavailable_handler)
app.add_exception_handler(UnacceptableFileError, unacceptable_file_handler)
app.add_exception_handler(DuplicateMaterialError, duplicate_material_handler)
app.add_exception_handler(CreditsExhaustedError, credits_exhausted_handler)
app.add_exception_handler(ConnectorLimitError, connector_limit_handler)
app.add_exception_handler(TariffConnectorLimitError, connector_limit_handler)
app.add_exception_handler(ConnectorNotInTariffError, connector_limit_handler)
app.add_exception_handler(InvalidConnectorConfigError, invalid_connector_config_handler)

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
app.add_middleware(MetricsMiddleware)
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
