from typing import Annotated

from fastapi import APIRouter, Depends, status

from corp_ed.api.v1.dependencies import (
    get_auth_service,
    get_current_user,
    get_current_user_allow_password_change,
    get_tenant_repository,
)
from corp_ed.api.v1.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    MeResponse,
    RefreshRequest,
    TokenResponse,
)
from corp_ed.domain.models import User
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.services.auth_service import AuthService, TokenPair

router = APIRouter(prefix="/auth", tags=["auth"])


def _tokens(pair: TokenPair) -> TokenResponse:
    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
    )


@router.get("/me", response_model=MeResponse)
async def read_me(
    current_user: Annotated[User, Depends(get_current_user_allow_password_change)],
    tenant_repo: Annotated[TenantRepository, Depends(get_tenant_repository)],
) -> MeResponse:
    """Кто вошёл. Доступна и до смены временного пароля: фронту нужно
    знать must_change_password, чтобы показать форму смены."""
    tenant = await tenant_repo.get_by_id(current_user.tenant_id)
    return MeResponse(
        id=current_user.id,
        email=current_user.email,
        full_name=current_user.full_name,
        role=current_user.role,
        tenant_id=current_user.tenant_id,
        company_name=tenant.name if tenant else "",
        must_change_password=current_user.must_change_password,
        last_login_at=current_user.last_login_at,
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    data: LoginRequest,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> TokenResponse:
    pair = await auth_service.login(data.company_code, data.email, data.password)
    return _tokens(pair)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    data: RefreshRequest,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> TokenResponse:
    return _tokens(await auth_service.refresh(data.refresh_token))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    data: RefreshRequest,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    current_user: Annotated[User, Depends(get_current_user_allow_password_change)],
) -> None:
    await auth_service.logout(current_user, data.refresh_token)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_everywhere(
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    await auth_service.logout_everywhere(current_user)


@router.post("/change-password", response_model=TokenResponse)
async def change_password(
    data: ChangePasswordRequest,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    current_user: Annotated[User, Depends(get_current_user_allow_password_change)],
) -> TokenResponse:
    """Сменить пароль. Все прежние сессии закрываются, возвращается
    новая пара токенов для текущего устройства."""
    pair = await auth_service.change_password(
        current_user, data.current_password, data.new_password
    )
    return _tokens(pair)
