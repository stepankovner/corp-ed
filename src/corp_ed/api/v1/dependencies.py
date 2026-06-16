from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.database import get_session
from corp_ed.core.exceptions import NotAuthenticatedError, PermissionError
from corp_ed.core.security import decode_access_token
from corp_ed.core.tenant_context import current_tenant
from corp_ed.domain.models import User, UserRole
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services.auth_service import AuthService
from corp_ed.services.user_service import UserService

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


def get_user_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserRepository:
    return UserRepository(session)


def get_user_service(
    repository: Annotated[UserRepository, Depends(get_user_repository)],
) -> UserService:
    return UserService(repository)


def get_tenant_repository(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TenantRepository:
    return TenantRepository(session)


def get_auth_service(
    tenant_repo: Annotated[TenantRepository, Depends(get_tenant_repository)],
    user_repo: Annotated[UserRepository, Depends(get_user_repository)],
) -> AuthService:
    return AuthService(tenant_repo, user_repo)


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    user_repo: Annotated[UserRepository, Depends(get_user_repository)],
) -> User:
    """Зависимость защищённых эндпоинтов: проверяет токен, возвращает User.

    Ставит tenant в контекст ИЗ ТОКЕНА (не из заголовка-заглушки) — это и есть
    боевая изоляция: подменить tenant нельзя, он внутри подписанного токена.
    """
    # Шаг 1-2: декодировать токен и проверить подпись.
    # decode кинет исключение, если токен битый или истёк (exp).
    try:
        payload = decode_access_token(token)
    except Exception as exc:
        raise NotAuthenticatedError("Невалидный или истёкший токен") from exc

    # Шаг 3: достать данные из payload, строки -> UUID.
    user_id_raw = payload.get("sub")
    tenant_id_raw = payload.get("tenant_id")
    if user_id_raw is None or tenant_id_raw is None:
        raise NotAuthenticatedError("Токен без обязательных полей")

    user_id = UUID(user_id_raw)
    tenant_id = UUID(tenant_id_raw)

    # Шаг 4: сначала ставим tenant в контекст — ИЗ ТОКЕНА, не из заголовка.
    current_tenant.set(tenant_id)
    # теперь хук изоляции при SELECT увидит правильный tenant и добавит WHERE tenant_id
    user = await user_repo.get_by_id(user_id)

    # проверить, что юзер существует и активен
    if user is None or not user.is_active:
        raise NotAuthenticatedError("Пользователь не найден или неактивен")

    return user


def require_role(*allowed_roles: UserRole) -> Callable[[User], User]:
    """Фабрика зависимостей: возвращает зависимость, которая пускает только
    юзеров с одной из перечисленных ролей. Иначе — 403.

    Пример использования на эндпоинте:
        current_user: Annotated[User, Depends(require_role(UserRole.MANAGER))]
    """

    def checker(
        current_user: Annotated[User, Depends(get_current_user)],
    ) -> User:
        if current_user.role not in allowed_roles:
            raise PermissionError("Недостаточно прав")
        return current_user

    return checker
