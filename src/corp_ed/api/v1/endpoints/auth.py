from typing import Annotated

from fastapi import APIRouter, Depends

from corp_ed.api.v1.dependencies import (
    get_auth_service,
    get_current_user,
    get_tenant_repository,
    require_role,
)
from corp_ed.api.v1.schemas.auth import LoginRequest, MeResponse, TokenResponse
from corp_ed.domain.models import User, UserRole
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me", response_model=MeResponse)
async def read_me(
    current_user: Annotated[User, Depends(get_current_user)],
    tenant_repo: Annotated[TenantRepository, Depends(get_tenant_repository)],
) -> MeResponse:
    """Кто вошёл и в какой компании.

    Название компании читается здесь, а не хранится в токене: токен
    подписан и не переиздаётся при переименовании компании.
    """
    tenant = await tenant_repo.get_by_id(current_user.tenant_id)

    return MeResponse(
        id=current_user.id,
        email=current_user.email,
        full_name=current_user.full_name,
        role=current_user.role,
        tenant_id=current_user.tenant_id,
        company_name=tenant.name if tenant else "",
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    data: LoginRequest,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> TokenResponse:
    token = await auth_service.login(data.company_code, data.email, data.password)
    return TokenResponse(access_token=token)


@router.get("/manager-only")
async def manager_only(
    current_user: Annotated[User, Depends(require_role(UserRole.MANAGER))],
) -> dict[str, str]:
    return {"message": f"Привет, {current_user.email}, тебе сюда можно"}
