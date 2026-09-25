import asyncio
from collections.abc import Callable
from functools import lru_cache
from typing import Annotated
from uuid import UUID

import httpx
import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.config import (
    BillingSettings,
    LLMSettings,
    RagSettings,
    get_billing_settings,
)
from corp_ed.core.database import get_session
from corp_ed.core.exceptions import (
    NotAuthenticatedError,
    PasswordChangeRequiredError,
    PermissionError,
)
from corp_ed.core.security import decode_access_token
from corp_ed.core.tenant_context import current_tenant
from corp_ed.domain.models import User, UserRole
from corp_ed.domain.types import Retriever
from corp_ed.llm.embedding_gateway import EmbeddingGateway
from corp_ed.llm.factory import build_llm_gateway
from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.throttle import Throttle
from corp_ed.llm.yandex_embedding import YandexEmbeddingAdapter
from corp_ed.repositories.audit_repository import AuditRepository
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.repositories.gap_repository import GapRepository
from corp_ed.repositories.glossary_repository import GlossaryRepository
from corp_ed.repositories.ingest_job_repository import IngestJobRepository
from corp_ed.repositories.material_repository import MaterialRepository
from corp_ed.repositories.qa_log_repository import QaLogRepository
from corp_ed.repositories.refresh_token_repository import RefreshTokenRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services.auth_service import AuthService
from corp_ed.services.credit_service import CreditService
from corp_ed.services.faq_service import FaqService
from corp_ed.services.gap_service import GapService
from corp_ed.services.glossary_service import GlossaryService
from corp_ed.services.material_service import MaterialService
from corp_ed.services.user_service import UserService

# auto_error=False: без заголовка FastAPI отдал бы свой 403. Отсутствие
# токена — это «не доказал, кто ты», то есть 401 с WWW-Authenticate,
# и его формирует наш обработчик NotAuthenticatedError.
bearer_scheme = HTTPBearer(auto_error=False)


def get_user_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserRepository:
    return UserRepository(session)


def get_tenant_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TenantRepository:
    return TenantRepository(session)


def get_refresh_token_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RefreshTokenRepository:
    return RefreshTokenRepository(session)


def get_audit_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AuditRepository:
    return AuditRepository(session)


def get_auth_service(
    tenant_repo: Annotated[TenantRepository, Depends(get_tenant_repository)],
    user_repo: Annotated[UserRepository, Depends(get_user_repository)],
    refresh_repo: Annotated[
        RefreshTokenRepository, Depends(get_refresh_token_repository)
    ],
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AuthService:
    return AuthService(tenant_repo, user_repo, refresh_repo, audit, session)


def get_user_service(
    user_repo: Annotated[UserRepository, Depends(get_user_repository)],
    refresh_repo: Annotated[
        RefreshTokenRepository, Depends(get_refresh_token_repository)
    ],
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserService:
    return UserService(user_repo, refresh_repo, audit, session)


async def get_current_user_allow_password_change(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    user_repo: Annotated[UserRepository, Depends(get_user_repository)],
    tenant_repo: Annotated[TenantRepository, Depends(get_tenant_repository)],
) -> User:
    """Проверить токен и вернуть пользователя — даже с временным паролем.

    Ставит tenant в контекст ИЗ ТОКЕНА (не из заголовка-заглушки) — это и есть
    боевая изоляция: подменить tenant нельзя, он внутри подписанного токена.

    Любая проблема с токеном — 401 с одним и тем же смыслом «сессия
    недействительна». 500 здесь недопустим: он сообщает атакующему, что
    подпись прошла, а дальше что-то сломалось.
    """
    if credentials is None:
        raise NotAuthenticatedError()

    # Шаг 1-2: подпись, срок, издатель, аудитория, тип токена.
    try:
        payload = decode_access_token(credentials.credentials)
    except jwt.PyJWTError as exc:
        raise NotAuthenticatedError("Невалидный или истёкший токен") from exc

    # Шаг 3: данные из payload. Токен подписан нами, но формат полей
    # всё равно проверяется: ключ мог утечь, а код — поменяться.
    try:
        user_id = UUID(payload["sub"])
        tenant_id = UUID(payload["tenant_id"])
        token_version = int(payload["ver"])
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise NotAuthenticatedError("Токен без обязательных полей") from exc

    # Шаг 4: сначала ставим tenant в контекст — ИЗ ТОКЕНА, не из заголовка.
    current_tenant.set(tenant_id)
    # теперь хук изоляции при SELECT увидит правильный tenant и добавит WHERE tenant_id
    tenant = await tenant_repo.get_by_id(tenant_id)
    user = await user_repo.get_by_id(user_id)

    # Шаг 5: компания и пользователь активны, токен не отозван сменой
    # пароля, роли, блокировкой или «выйти везде».
    if (
        tenant is None
        or not tenant.is_active
        or user is None
        # Явная сверка, а не только хук: изоляция не должна держаться на
        # одном механизме (см. DECISIONS.md, session.get и identity map).
        or user.tenant_id != tenant_id
        or not user.is_active
        or user.token_version != token_version
    ):
        raise NotAuthenticatedError("Сессия недействительна")

    return user


async def get_current_user(
    user: Annotated[User, Depends(get_current_user_allow_password_change)],
) -> User:
    """Зависимость защищённых эндпоинтов: проверяет токен, возвращает User.

    Пользователь с временным паролем сюда не проходит (403): временный
    пароль знает администратор, выдавший его.
    """
    if user.must_change_password:
        raise PasswordChangeRequiredError()
    return user


def require_role(*allowed_roles: UserRole) -> Callable[[User], User]:
    """Фабрика зависимостей: возвращает зависимость, которая пускает только
    юзеров с одной из перечисленных ролей. Иначе — 403.

    Пример использования на эндпоинте:
        current_user: Annotated[User, Depends(require_role(UserRole.ADMIN))]
    """

    def checker(
        current_user: Annotated[User, Depends(get_current_user)],
    ) -> User:
        if current_user.role not in allowed_roles:
            raise PermissionError("Недостаточно прав")
        return current_user

    return checker


@lru_cache
def get_llm_settings() -> LLMSettings:
    return LLMSettings()  # type: ignore[call-arg]


@lru_cache
def get_rag_settings() -> RagSettings:
    return RagSettings()  # type: ignore[call-arg]


def get_http_client(request: Request) -> httpx.AsyncClient:
    client: httpx.AsyncClient = request.app.state.http_client
    return client


def get_llm_semaphore(request: Request) -> asyncio.Semaphore | None:
    semaphore: asyncio.Semaphore | None = getattr(
        request.app.state, "llm_semaphore", None
    )
    return semaphore


def get_query_throttle(request: Request) -> Throttle | None:
    throttle: Throttle | None = getattr(
        request.app.state, "embedding_query_throttle", None
    )
    return throttle


def get_embedding_gateway(
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    settings: Annotated[LLMSettings, Depends(get_llm_settings)],
    query_throttle: Annotated[Throttle | None, Depends(get_query_throttle)],
) -> EmbeddingGateway:
    """В API эмбеддинги нужны только для вопросов; документы считает воркер."""
    return YandexEmbeddingAdapter(
        client=client,
        folder_id=settings.yc_folder_id,
        api_key=settings.yc_api_key.get_secret_value(),
        family=settings.embedding_model,
        dim=settings.embedding_dim,
        query_throttle=query_throttle,
    )


def get_llm_gateway(
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    settings: Annotated[LLMSettings, Depends(get_llm_settings)],
    semaphore: Annotated[asyncio.Semaphore | None, Depends(get_llm_semaphore)],
) -> LLMGateway:
    return build_llm_gateway(client, settings, semaphore)


def get_material_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MaterialRepository:
    return MaterialRepository(session)


def get_chunk_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ChunkRepository:
    return ChunkRepository(session)


def get_ingest_job_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> IngestJobRepository:
    return IngestJobRepository(session)


def get_material_service(
    material_repo: Annotated[MaterialRepository, Depends(get_material_repository)],
    job_repo: Annotated[IngestJobRepository, Depends(get_ingest_job_repository)],
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MaterialService:
    return MaterialService(
        material_repo=material_repo,
        job_repo=job_repo,
        audit=audit,
        session=session,
    )


def get_qa_log_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> QaLogRepository:
    return QaLogRepository(session)


def get_glossary_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> GlossaryRepository:
    return GlossaryRepository(session)


def get_glossary_service(
    repository: Annotated[GlossaryRepository, Depends(get_glossary_repository)],
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> GlossaryService:
    return GlossaryService(repository, audit, session)


def get_gap_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
) -> GapService:
    return GapService(GapRepository(session), audit, session)


def get_credit_service(
    tenant_repo: Annotated[TenantRepository, Depends(get_tenant_repository)],
    qa_log_repo: Annotated[QaLogRepository, Depends(get_qa_log_repository)],
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
    settings: Annotated[BillingSettings, Depends(get_billing_settings)],
) -> CreditService:
    return CreditService(
        tenant_repo,
        qa_log_repo,
        audit,
        credits_per_seat=settings.credits_per_seat,
        tokens_per_credit=settings.tokens_per_credit,
        zone=settings.zone,
        warn_at_percent=settings.warn_at_percent,
    )


def get_faq_service(
    chunk_repo: Annotated[ChunkRepository, Depends(get_chunk_repository)],
    qa_log_repo: Annotated[QaLogRepository, Depends(get_qa_log_repository)],
    tenant_repo: Annotated[TenantRepository, Depends(get_tenant_repository)],
    glossary_repo: Annotated[GlossaryRepository, Depends(get_glossary_repository)],
    credits: Annotated[CreditService, Depends(get_credit_service)],
    embedding_gateway: Annotated[EmbeddingGateway, Depends(get_embedding_gateway)],
    llm_gateway: Annotated[LLMGateway, Depends(get_llm_gateway)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[RagSettings, Depends(get_rag_settings)],
) -> FaqService:
    return FaqService(
        chunk_repo=chunk_repo,
        qa_log_repo=qa_log_repo,
        tenant_repo=tenant_repo,
        glossary_repo=glossary_repo,
        credits=credits,
        embedding_gateway=embedding_gateway,
        llm_gateway=llm_gateway,
        session=session,
        limit=settings.faq_limit,
        max_distance=settings.faq_max_distance,
        context_max_tokens=settings.context_max_tokens,
        temperature=settings.faq_temperature,
        retriever=Retriever(settings.retriever),
        fulltext_weight=settings.fulltext_weight,
    )
