from collections.abc import Callable
from functools import lru_cache
from typing import Annotated
from uuid import UUID

import httpx
import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.config import LLMSettings, RagSettings
from corp_ed.core.database import get_session
from corp_ed.core.exceptions import (
    NotAuthenticatedError,
    PasswordChangeRequiredError,
    PermissionError,
)
from corp_ed.core.security import decode_access_token
from corp_ed.core.tenant_context import current_tenant
from corp_ed.domain.models import User, UserRole
from corp_ed.llm.embedding_gateway import EmbeddingGateway
from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.yandex import YandexAdapter
from corp_ed.llm.yandex_embedding import YandexEmbeddingAdapter
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.repositories.material_repository import MaterialRepository
from corp_ed.repositories.refresh_token_repository import RefreshTokenRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services.auth_service import AuthService
from corp_ed.services.faq_service import FaqService
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


def get_auth_service(
    tenant_repo: Annotated[TenantRepository, Depends(get_tenant_repository)],
    user_repo: Annotated[UserRepository, Depends(get_user_repository)],
    refresh_repo: Annotated[
        RefreshTokenRepository, Depends(get_refresh_token_repository)
    ],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AuthService:
    return AuthService(tenant_repo, user_repo, refresh_repo, session)


def get_user_service(
    user_repo: Annotated[UserRepository, Depends(get_user_repository)],
    refresh_repo: Annotated[
        RefreshTokenRepository, Depends(get_refresh_token_repository)
    ],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserService:
    return UserService(user_repo, refresh_repo, session)


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


def get_embedding_gateway(
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    settings: Annotated[LLMSettings, Depends(get_llm_settings)],
) -> EmbeddingGateway:
    return YandexEmbeddingAdapter(
        client=client,
        folder_id=settings.yc_folder_id,
        api_key=settings.yc_api_key,
    )


def get_llm_gateway(
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    settings: Annotated[LLMSettings, Depends(get_llm_settings)],
) -> LLMGateway:
    return YandexAdapter(
        client=client,
        folder_id=settings.yc_folder_id,
        api_key=settings.yc_api_key,
    )


def get_material_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MaterialRepository:
    return MaterialRepository(session)


def get_chunk_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ChunkRepository:
    return ChunkRepository(session)


def get_material_service(
    material_repo: Annotated[
        MaterialRepository,
        Depends(get_material_repository),
    ],
    chunk_repo: Annotated[
        ChunkRepository,
        Depends(get_chunk_repository),
    ],
    embedding_gateway: Annotated[
        EmbeddingGateway,
        Depends(get_embedding_gateway),
    ],
    session: Annotated[
        AsyncSession,
        Depends(get_session),
    ],
    settings: Annotated[
        RagSettings,
        Depends(get_rag_settings),
    ],
) -> MaterialService:
    return MaterialService(
        material_repo=material_repo,
        chunk_repo=chunk_repo,
        embedding_gateway=embedding_gateway,
        session=session,
        chunk_tokens=settings.chunk_tokens,
        overlap_tokens=settings.overlap_tokens,
    )


def get_faq_service(
    chunk_repo: Annotated[
        ChunkRepository,
        Depends(get_chunk_repository),
    ],
    embedding_gateway: Annotated[
        EmbeddingGateway,
        Depends(get_embedding_gateway),
    ],
    llm_gateway: Annotated[
        LLMGateway,
        Depends(get_llm_gateway),
    ],
    settings: Annotated[
        RagSettings,
        Depends(get_rag_settings),
    ],
) -> FaqService:
    return FaqService(
        chunk_repo=chunk_repo,
        embedding_gateway=embedding_gateway,
        llm_gateway=llm_gateway,
        limit=settings.faq_limit,
        max_distance=settings.faq_max_distance,
        context_max_tokens=settings.context_max_tokens,
        temperature=settings.faq_temperature,
    )
