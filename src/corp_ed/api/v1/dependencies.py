from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.database import get_session
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services.auth_service import AuthService
from corp_ed.services.user_service import UserService


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
