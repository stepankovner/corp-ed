import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
from typing import Annotated
from uuid import UUID

import httpx
import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.registry import AdapterRegistry, default_registry
from corp_ed.core.config import (
    BillingSettings,
    ConnectorSettings,
    LLMSettings,
    RagSettings,
    get_auth_settings,
    get_billing_settings,
    get_connector_settings,
    get_demo_settings,
    get_lead_settings,
    get_settings,
)
from corp_ed.core.database import get_session, get_session_maker
from corp_ed.core.dialogue_store import DialogueStore
from corp_ed.core.exceptions import (
    MfaSetupRequiredError,
    NoCompanyError,
    NotAuthenticatedError,
    NotFoundError,
    PasswordChangeRequiredError,
    PermissionError,
)
from corp_ed.core.outbound import OutboundClient
from corp_ed.core.rate_limit import RateLimiter
from corp_ed.core.secrets import SecretBox
from corp_ed.core.security import decode_access_token
from corp_ed.core.tenant_context import current_account, current_staff, current_tenant
from corp_ed.domain.models import (
    Account,
    MemberStatus,
    Passkey,
    StaffMember,
    Tenant,
    User,
    UserRole,
)
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
from corp_ed.repositories.department_repository import DepartmentRepository
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
from corp_ed.services.analytics_service import AnalyticsService
from corp_ed.services.attachment_service import AttachmentService
from corp_ed.services.auth_service import AuthService
from corp_ed.services.avatar_service import AvatarService
from corp_ed.services.chat_generation import (
    ChatGenerator,
    ChatRunner,
    InMemoryStopSignals,
    StopSignals,
)
from corp_ed.services.chat_service import ChatService
from corp_ed.services.company_request_service import CompanyRequestService
from corp_ed.services.company_service import CompanyService
from corp_ed.services.connector_service import ConnectorService
from corp_ed.services.credit_service import CreditService
from corp_ed.services.demo_service import DemoService
from corp_ed.services.department_service import DepartmentService
from corp_ed.services.faq_service import FaqService
from corp_ed.services.folder_service import FolderService
from corp_ed.services.gap_service import GapService
from corp_ed.services.general_answer import ModelKnowledgeSource
from corp_ed.services.glossary_service import GlossaryService
from corp_ed.services.invite_service import InviteService
from corp_ed.services.lead_service import LeadService
from corp_ed.services.material_service import MaterialService
from corp_ed.services.mfa_service import MfaService, RelyingParty
from corp_ed.services.notification_service import NotificationService
from corp_ed.services.onboarding_service import OnboardingService
from corp_ed.services.people_service import PeopleService
from corp_ed.services.sources_service import SourcesService
from corp_ed.services.staff_service import StaffService
from corp_ed.services.suggestion_service import SuggestionService
from corp_ed.services.support_service import SupportService
from corp_ed.services.team_notify import NULL_NOTIFIER, TeamNotifier
from corp_ed.services.tenant_service import TenantService
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
    session_id: UUID
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
        session_id = UUID(payload["sid"])
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
    # Шаг 4б: сеанс не закрыт — «Выйти» и «Завершить» в списке сеансов
    # действуют сразу, а не когда истечёт токен (до 15 минут).
    if await RefreshTokenRepository(session).family_revoked(session_id):
        raise NotAuthenticatedError("Сессия недействительна")
    if tenant_id is None:
        return Principal(account=account, member=None, session_id=session_id)

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

    return Principal(
        account=account, member=member, session_id=session_id, tenant=tenant
    )


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


async def get_staff_account(
    principal: Annotated[Principal, Depends(get_principal)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Account:
    """Наша панель (ТЗ §9): только команда kronto (staff_members, заводит
    CLI) и только с приложением или ключом доступа.

    Не из команды — 404, как у несуществующей ручки: панель не выдаёт,
    что она есть. Учётка команды попадает в журнал действий
    (current_staff) — членства в чужой компании у неё нет.
    """
    account = principal.account
    if await session.get(StaffMember, account.id) is None:
        raise NotFoundError("Not Found")
    if not await _has_strong_factor(session, account):
        raise MfaSetupRequiredError()
    current_staff.set(account.id)
    return account


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


def get_outbound_http_client(request: Request) -> httpx.AsyncClient:
    """Клиент для запросов наружу (системы клиентов): прокси из окружения
    — только с CONNECTOR_OUTBOUND_VIA_PROXY (core/outbound.py)."""
    client: httpx.AsyncClient = request.app.state.outbound_http_client
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


def get_document_throttle(request: Request) -> Throttle | None:
    throttle: Throttle | None = getattr(
        request.app.state, "embedding_ingest_throttle", None
    )
    return throttle


def get_embedding_gateway(
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    settings: Annotated[LLMSettings, Depends(get_llm_settings)],
    query_throttle: Annotated[Throttle | None, Depends(get_query_throttle)],
    document_throttle: Annotated[Throttle | None, Depends(get_document_throttle)],
) -> EmbeddingGateway:
    """Вопросы — в своей доле квоты; документы компании считает воркер, а
    API — только вложения к вопросу (ТЗ §6), в общей с воркером доле
    ингеста."""
    return build_embedding_gateway(
        client,
        settings,
        query_throttle=query_throttle,
        document_throttle=document_throttle,
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


FaqBuilder = Callable[[AsyncSession], FaqService]
"""FaqService на заданной сессии: у фоновой задачи чата (ТЗ §6) своя
сессия БД — сессия запроса закрывается раньше, чем дописан ответ."""


def get_faq_builder(
    embedding_gateway: Annotated[EmbeddingGateway, Depends(get_embedding_gateway)],
    llm_gateway: Annotated[LLMGateway, Depends(get_llm_gateway)],
    settings: Annotated[RagSettings, Depends(get_rag_settings)],
    billing: Annotated[BillingSettings, Depends(get_billing_settings)],
    notifier: Annotated[TeamNotifier, Depends(get_team_notifier)],
    reranker: Annotated[Reranker | None, Depends(get_reranker)],
    dialogue_store: Annotated[DialogueStore | None, Depends(get_dialogue_store)],
) -> FaqBuilder:
    def build(session: AsyncSession) -> FaqService:
        tenant_repo = TenantRepository(session)
        qa_log_repo = QaLogRepository(session)
        credits = CreditService(
            tenant_repo,
            qa_log_repo,
            AuditRepository(session),
            credits_per_seat=billing.credits_per_seat,
            tokens_per_credit=billing.tokens_per_credit,
            zone=billing.zone,
            warn_at_percent=billing.warn_at_percent,
            notifier=notifier,
        )
        return FaqService(
            chunk_repo=ChunkRepository(session),
            qa_log_repo=qa_log_repo,
            tenant_repo=tenant_repo,
            glossary_repo=GlossaryRepository(session),
            credits=credits,
            embedding_gateway=embedding_gateway,
            llm_gateway=llm_gateway,
            session=session,
            limit=settings.faq_limit,
            max_distance=settings.faq_max_distance,
            gate_distance=settings.faq_gate_distance,
            near_margin=settings.faq_near_margin,
            context_max_tokens=settings.context_max_tokens,
            temperature=settings.faq_temperature,
            retriever=Retriever(settings.retriever),
            fulltext_weight=settings.fulltext_weight,
            # Поиск в интернете после MVP — другой GeneralAnswerSource здесь.
            general_source=ModelKnowledgeSource(
                llm_gateway, temperature=settings.faq_temperature
            ),
            # Память в Redis — только у /faq/ask; чат передаёт историю
            # из своих диалогов сам (ChatService).
            dialogue_store=dialogue_store,
            history_turns=settings.history_turns,
            history_ttl_minutes=settings.history_ttl_minutes,
            condense_timeout=settings.condense_timeout_seconds,
            reranker=reranker,
            rerank_depth=settings.rerank_depth,
            rerank_timeout=settings.rerank_timeout_ms / 1000,
            rerank_max_words=settings.rerank_max_words,
        )

    return build


def get_faq_service(
    build: Annotated[FaqBuilder, Depends(get_faq_builder)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> FaqService:
    return build(session)


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Фабрика сессий для работы вне запроса (фоновый ответ чата)."""
    return get_session_maker()


_DEFAULT_STOP_SIGNALS = InMemoryStopSignals()
_DEFAULT_CHAT_RUNNER = ChatRunner()


def get_stop_signals(request: Request) -> StopSignals:
    """«Остановить» ответ: Redis в бою (поток и просьба могут попасть в
    разные процессы), память процесса без Redis и в тестах."""
    signals: StopSignals = getattr(
        request.app.state, "chat_stop_signals", _DEFAULT_STOP_SIGNALS
    )
    return signals


def get_chat_runner(request: Request) -> ChatRunner:
    runner: ChatRunner = getattr(request.app.state, "chat_runner", _DEFAULT_CHAT_RUNNER)
    return runner


def get_chat_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    credits: Annotated[CreditService, Depends(get_credit_service)],
    settings: Annotated[RagSettings, Depends(get_rag_settings)],
) -> ChatService:
    return ChatService(
        session,
        credits,
        history_turns=settings.history_turns,
        share_ttl=timedelta(days=get_settings().chat_share_ttl_days),
    )


def get_chat_generator(
    session_factory: Annotated[
        async_sessionmaker[AsyncSession], Depends(get_session_factory)
    ],
    build: Annotated[FaqBuilder, Depends(get_faq_builder)],
    stop: Annotated[StopSignals, Depends(get_stop_signals)],
) -> ChatGenerator:
    return ChatGenerator(session_factory, build, stop)


def get_demo_service(
    session_factory: Annotated[
        async_sessionmaker[AsyncSession], Depends(get_session_factory)
    ],
    build: Annotated[FaqBuilder, Depends(get_faq_builder)],
) -> DemoService:
    """Песочница на сайте (ТЗ §1): своя сессия в области компании песочницы
    — у запроса без входа компании нет."""
    return DemoService(session_factory, get_demo_settings(), build)


def get_attachment_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    embedding_gateway: Annotated[EmbeddingGateway, Depends(get_embedding_gateway)],
    settings: Annotated[RagSettings, Depends(get_rag_settings)],
) -> AttachmentService:
    return AttachmentService(
        session,
        embedding_gateway,
        chunk_tokens=settings.chunk_tokens,
        overlap_tokens=settings.overlap_tokens,
    )


def get_company_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
    notifier: Annotated[TeamNotifier, Depends(get_team_notifier)],
) -> CompanyService:
    return CompanyService(session, audit, notifier)


def get_analytics_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    billing: Annotated[BillingSettings, Depends(get_billing_settings)],
) -> AnalyticsService:
    # Сутки — по тому же поясу, что и месяц расхода (московское время).
    return AnalyticsService(session, zone=billing.billing_timezone)


def get_folder_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
) -> FolderService:
    return FolderService(session, audit)


def get_sources_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SourcesService:
    return SourcesService(session)


def get_suggestion_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
) -> SuggestionService:
    return SuggestionService(session, audit)


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


def get_avatar_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AvatarService:
    return AvatarService(session)


def get_department_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DepartmentRepository:
    return DepartmentRepository(session)


def get_department_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    repository: Annotated[DepartmentRepository, Depends(get_department_repository)],
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
) -> DepartmentService:
    return DepartmentService(repository, audit, session)


def get_people_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    user_repo: Annotated[UserRepository, Depends(get_user_repository)],
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
) -> PeopleService:
    return PeopleService(
        user_repo, DepartmentRepository(session), AvatarService(session), audit, session
    )


def get_account_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
    mfa: Annotated[MfaService, Depends(get_mfa_service)],
) -> AccountService:
    return AccountService(session, auth_service, audit, mfa)


def get_outbound_client(
    client: Annotated[httpx.AsyncClient, Depends(get_outbound_http_client)],
) -> OutboundClient:
    settings = get_connector_settings()
    return OutboundClient(
        client,
        via_proxy=settings.outbound_via_proxy,
        download_deadline=settings.download_timeout_seconds,
    )


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


def get_staff_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    session_maker: Annotated[
        async_sessionmaker[AsyncSession], Depends(get_session_factory)
    ],
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
    billing: Annotated[BillingSettings, Depends(get_billing_settings)],
) -> StaffService:
    return StaffService(
        session,
        session_maker,
        audit,
        zone=billing.zone,
        credits_per_seat=billing.credits_per_seat,
        rub_per_1k_tokens=billing.llm_rub_per_1k_tokens,
    )


def get_tenant_service(
    tenant_repo: Annotated[TenantRepository, Depends(get_tenant_repository)],
    user_repo: Annotated[UserRepository, Depends(get_user_repository)],
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TenantService:
    """Тариф, места, приостановка — то же, что cli, для нашей панели."""
    return TenantService(tenant_repo, user_repo, audit, session)


def get_notification_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> NotificationService:
    return NotificationService(session)


def get_onboarding_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> OnboardingService:
    return OnboardingService(session)


def get_support_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
    notifier: Annotated[TeamNotifier, Depends(get_team_notifier)],
) -> SupportService:
    return SupportService(session, audit, notifier)
