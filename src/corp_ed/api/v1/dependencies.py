import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated
from uuid import UUID

import httpx
import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.connectors.registry import AdapterRegistry, default_registry
from corp_ed.core.config import (
    BillingSettings,
    ConnectorSettings,
    LLMSettings,
    RagSettings,
    get_auth_settings,
    get_billing_settings,
    get_connector_settings,
    get_lead_settings,
)
from corp_ed.core.database import get_session
from corp_ed.core.dialogue_store import DialogueStore
from corp_ed.core.exceptions import (
    MfaSetupRequiredError,
    NoCompanyError,
    NotAuthenticatedError,
    PasswordChangeRequiredError,
    PermissionError,
)
from corp_ed.core.outbound import OutboundClient
from corp_ed.core.rate_limit import RateLimiter
from corp_ed.core.secrets import SecretBox
from corp_ed.core.security import decode_access_token
from corp_ed.core.tenant_context import current_account, current_tenant
from corp_ed.domain.models import Account, MemberStatus, Passkey, Tenant, User, UserRole
from corp_ed.domain.types import Retriever
from corp_ed.llm.embedding_gateway import EmbeddingGateway
from corp_ed.llm.factory import build_embedding_gateway, build_llm_gateway
from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.reranker import HttpReranker, Reranker
from corp_ed.llm.throttle import Throttle
from corp_ed.repositories.account_repository import AccountRepository
from corp_ed.repositories.audit_repository import AuditRepository
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.repositories.connector_repository import (
    ConnectorRepository,
    GrantRepository,
    SyncRunRepository,
)
from corp_ed.repositories.connector_sync_job_repository import (
    ConnectorSyncJobRepository,
)
from corp_ed.repositories.gap_repository import GapRepository
from corp_ed.repositories.glossary_repository import GlossaryRepository
from corp_ed.repositories.ingest_job_repository import IngestJobRepository
from corp_ed.repositories.invite_repository import InviteRepository
from corp_ed.repositories.lead_repository import LeadRepository
from corp_ed.repositories.material_repository import MaterialRepository
from corp_ed.repositories.qa_log_repository import QaLogRepository
from corp_ed.repositories.refresh_token_repository import RefreshTokenRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services.account_service import AccountService
from corp_ed.services.auth_service import AuthService
from corp_ed.services.company_request_service import CompanyRequestService
from corp_ed.services.connector_service import ConnectorService
from corp_ed.services.credit_service import CreditService
from corp_ed.services.faq_service import FaqService
from corp_ed.services.gap_service import GapService
from corp_ed.services.general_answer import ModelKnowledgeSource
from corp_ed.services.glossary_service import GlossaryService
from corp_ed.services.invite_service import InviteService
from corp_ed.services.lead_service import LeadService
from corp_ed.services.material_service import MaterialService
from corp_ed.services.mfa_service import MfaService, RelyingParty
from corp_ed.services.team_notify import NULL_NOTIFIER, TeamNotifier
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


def get_invite_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    tenant_repo: Annotated[TenantRepository, Depends(get_tenant_repository)],
    user_repo: Annotated[UserRepository, Depends(get_user_repository)],
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
) -> InviteService:
    return InviteService(
        InviteRepository(session), tenant_repo, user_repo, audit, session
    )


def get_lead_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LeadService:
    return LeadService(LeadRepository(session), session, get_lead_settings())


def get_user_service(
    user_repo: Annotated[UserRepository, Depends(get_user_repository)],
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserService:
    return UserService(user_repo, audit, session)


def get_relying_party(request: Request) -> RelyingParty:
    """Сайт для ключей доступа (WebAuthn). Имя хоста — из заголовка Host,
    который уже проверил TrustedHost; адреса страниц — https этого хоста
    (http — только для localhost) или AUTH_WEBAUTHN_ORIGINS."""
    settings = get_auth_settings()
    host = request.headers.get("host", "")
    hostname = host.rsplit(":", 1)[0] if not host.startswith("[") else host
    rp_id = settings.webauthn_rp_id or hostname
    if settings.origins:
        origins = settings.origins
    else:
        scheme = "http" if hostname in ("localhost", "127.0.0.1") else "https"
        origins = [f"{scheme}://{host}"]
    return RelyingParty(id=rp_id, origins=origins)


@dataclass(frozen=True)
class Principal:
    """Кто вошёл: учётка и, если выбрана компания, членство и компания."""

    account: Account
    member: User | None
    tenant: Tenant | None = None


async def get_principal_allow_password_change(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
    user_repo: Annotated[UserRepository, Depends(get_user_repository)],
    tenant_repo: Annotated[TenantRepository, Depends(get_tenant_repository)],
) -> Principal:
    """Проверить токен и вернуть учётку с членством — даже с временным
    паролем.

    Учётку и компанию ставит в контекст ИЗ ТОКЕНА (не из заголовка) — это
    и есть боевая изоляция: подменить компанию нельзя, она внутри
    подписанного токена.

    Любая проблема с токеном — 401 с одним и тем же смыслом «сессия
    недействительна». 500 здесь недопустим: он сообщает атакующему, что
    подпись прошла, а дальше что-то сломалось. Членство кончилось
    (убрали, заблокировали) — тоже 401: фронт обновит токен и получит
    сессию без этой компании.
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
        account_id = UUID(payload["sub"])
        account_version = int(payload["ver"])
        tenant_raw = payload.get("tenant_id")
        tenant_id = UUID(tenant_raw) if tenant_raw is not None else None
        member_id = UUID(payload["member_id"]) if tenant_id is not None else None
        member_version = int(payload["mver"]) if tenant_id is not None else None
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise NotAuthenticatedError("Токен без обязательных полей") from exc

    # Шаг 4: учётка жива, токен не отозван сменой пароля или «выйти везде».
    current_account.set(account_id)
    account = await AccountRepository(session).get(account_id)
    if account is None or account.token_version != account_version:
        raise NotAuthenticatedError("Сессия недействительна")
    if tenant_id is None:
        return Principal(account=account, member=None)

    # Шаг 5: компания — в контекст ИЗ ТОКЕНА; хук изоляции добавит
    # WHERE tenant_id ко всем запросам ниже.
    current_tenant.set(tenant_id)
    tenant = await tenant_repo.get_by_id(tenant_id)
    member = await user_repo.get_by_id(member_id) if member_id else None

    # Шаг 6: компания активна, членство этой учётки действует и не
    # отозвано сменой роли, блокировкой или удалением из компании.
    if (
        tenant is None
        or not tenant.is_active
        or member is None
        # Явная сверка, а не только хук: изоляция не должна держаться на
        # одном механизме (см. DECISIONS.md, session.get и identity map).
        or member.tenant_id != tenant_id
        or member.account_id != account.id
        or member.status is not MemberStatus.ACTIVE
        or member.token_version != member_version
    ):
        raise NotAuthenticatedError("Сессия недействительна")

    return Principal(account=account, member=member, tenant=tenant)


async def get_principal(
    principal: Annotated[Principal, Depends(get_principal_allow_password_change)],
) -> Principal:
    """Пароль, выданный командой (cli reset-password), сначала сменить:
    его видел кто-то кроме владельца."""
    if principal.account.must_change_password:
        raise PasswordChangeRequiredError()
    return principal


async def get_current_account(
    principal: Annotated[Principal, Depends(get_principal)],
) -> Account:
    """Ручки учётки (профиль, компании, вступление): компания не нужна."""
    return principal.account


async def get_current_user(
    principal: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    """Зависимость ручек компании: членство в выбранной компании.

    Нет выбранной компании — 403 no_company: фронт показывает экран
    «Вы ещё не в компании». Администратору и компании с правилом strong
    нужен надёжный второй фактор (ТЗ §3) — без него 403
    mfa_setup_required: фронт ведёт на настройку защиты.
    """
    member = principal.member
    if member is None:
        raise NoCompanyError()
    needs_strong = member.role is UserRole.ADMIN or (
        principal.tenant is not None and principal.tenant.mfa_policy == "strong"
    )
    if needs_strong and not await _has_strong_factor(session, principal.account):
        raise MfaSetupRequiredError()
    return member


async def _has_strong_factor(session: AsyncSession, account: Account) -> bool:
    if account.totp_enabled_at is not None:
        return True
    count = await session.scalar(
        select(func.count())
        .select_from(Passkey)
        .where(Passkey.account_id == account.id)
    )
    return bool(count)


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
    return LLMSettings()


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
    return build_embedding_gateway(client, settings, query_throttle=query_throttle)


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


def get_team_notifier(request: Request) -> TeamNotifier:
    """Уведомления команде (П-5). В тестах lifespan не запускается —
    уведомлений нет."""
    notifier: TeamNotifier = getattr(request.app.state, "team_notifier", NULL_NOTIFIER)
    return notifier


def get_company_request_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
    notifier: Annotated[TeamNotifier, Depends(get_team_notifier)],
) -> CompanyRequestService:
    return CompanyRequestService(session, audit, notifier)


def get_dialogue_store(request: Request) -> DialogueStore | None:
    """Реплики диалогов (BH-28): Redis в бою, память процесса без Redis.
    В тестах lifespan не запускается — памяти диалога нет, пока тест сам
    не положит хранилище в app.state."""
    store: DialogueStore | None = getattr(request.app.state, "dialogue_store", None)
    return store


def get_reranker(
    request: Request, settings: Annotated[RagSettings, Depends(get_rag_settings)]
) -> Reranker | None:
    """Реранкер (M3, BH-32): HTTP-сервис из compose.yaml или ничего —
    пустой RAG_RERANK_MODEL выключает его."""
    if not settings.rerank_model:
        return None
    return HttpReranker(
        get_http_client(request),
        settings.rerank_url,
        model=settings.rerank_model,
        timeout=settings.rerank_timeout_ms / 1000,
    )


def get_credit_service(
    tenant_repo: Annotated[TenantRepository, Depends(get_tenant_repository)],
    qa_log_repo: Annotated[QaLogRepository, Depends(get_qa_log_repository)],
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
    settings: Annotated[BillingSettings, Depends(get_billing_settings)],
    notifier: Annotated[TeamNotifier, Depends(get_team_notifier)],
) -> CreditService:
    return CreditService(
        tenant_repo,
        qa_log_repo,
        audit,
        credits_per_seat=settings.credits_per_seat,
        tokens_per_credit=settings.tokens_per_credit,
        zone=settings.zone,
        warn_at_percent=settings.warn_at_percent,
        notifier=notifier,
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
    dialogue_store: Annotated[DialogueStore | None, Depends(get_dialogue_store)],
    reranker: Annotated[Reranker | None, Depends(get_reranker)],
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
        # Поиск в интернете после MVP — другой GeneralAnswerSource здесь.
        general_source=ModelKnowledgeSource(
            llm_gateway, temperature=settings.faq_temperature
        ),
        dialogue_store=dialogue_store,
        history_turns=settings.history_turns,
        history_ttl_minutes=settings.history_ttl_minutes,
        condense_timeout=settings.condense_timeout_seconds,
        reranker=reranker,
        rerank_depth=settings.rerank_depth,
        rerank_timeout=settings.rerank_timeout_ms / 1000,
    )


@lru_cache
def get_adapter_registry() -> AdapterRegistry:
    return default_registry(get_connector_settings())


@lru_cache
def get_secret_box() -> SecretBox:
    return SecretBox(get_connector_settings().keys)


def get_mfa_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
    secrets: Annotated[SecretBox, Depends(get_secret_box)],
) -> MfaService:
    # Секрет TOTP шифруется тем же ключом, что учётные данные подключений
    # (CONNECTOR_SECRETS_KEYS): одно кольцо ключей на сервис.
    return MfaService(session, secrets, audit)


def get_account_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
    mfa: Annotated[MfaService, Depends(get_mfa_service)],
) -> AccountService:
    return AccountService(session, auth_service, audit, mfa)


def get_outbound_client(
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> OutboundClient:
    return OutboundClient(client, via_proxy=get_connector_settings().outbound_via_proxy)


def get_connector_service(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
    registry: Annotated[AdapterRegistry, Depends(get_adapter_registry)],
    secrets: Annotated[SecretBox, Depends(get_secret_box)],
    settings: Annotated[ConnectorSettings, Depends(get_connector_settings)],
    http: Annotated[OutboundClient, Depends(get_outbound_client)],
) -> ConnectorService:
    # Лимитер приложения (Redis или память) — для одноразовости state OAuth.
    limiter: RateLimiter | None = getattr(request.app.state, "rate_limiter", None)
    return ConnectorService(
        ConnectorRepository(session),
        GrantRepository(session),
        SyncRunRepository(session),
        ConnectorSyncJobRepository(session),
        MaterialRepository(session),
        audit,
        secrets,
        registry,
        settings,
        session,
        http,
        limiter=limiter,
    )
