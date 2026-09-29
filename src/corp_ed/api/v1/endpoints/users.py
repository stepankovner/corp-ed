from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from corp_ed.api.v1.dependencies import get_user_service, require_role
from corp_ed.api.v1.schemas.user import (
    PasswordResetResponse,
    UserCreatedResponse,
    UserCreateRequest,
    UserResponse,
    UserUpdateRequest,
)
from corp_ed.domain.models import User, UserRole
from corp_ed.services.user_service import UserService

router = APIRouter(prefix="/users", tags=["users"])

AdminUser = Annotated[User, Depends(require_role(UserRole.ADMIN))]


@router.get("", response_model=list[UserResponse])
async def list_users(
    service: Annotated[UserService, Depends(get_user_service)],
    current_user: AdminUser,
) -> list[UserResponse]:
    users = await service.list_users()
    return [UserResponse.model_validate(user) for user in users]


@router.post(
    "",
    response_model=UserCreatedResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_user(
    data: UserCreateRequest,
    service: Annotated[UserService, Depends(get_user_service)],
    current_user: AdminUser,
) -> UserCreatedResponse:
    created = await service.create_user(
        current_user,
        email=data.email,
        full_name=data.full_name,
        role=data.role,
        password=data.password,
    )
    return UserCreatedResponse(
        user=UserResponse.model_validate(created.user),
        temporary_password=created.temporary_password,
    )


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: UUID,
    data: UserUpdateRequest,
    service: Annotated[UserService, Depends(get_user_service)],
    current_user: AdminUser,
) -> UserResponse:
    user = await service.update_user(
        current_user,
        user_id,
        role=data.role,
        is_active=data.is_active,
        full_name=data.full_name,
    )
    return UserResponse.model_validate(user)


@router.post("/{user_id}/reset-password", response_model=PasswordResetResponse)
async def reset_password(
    user_id: UUID,
    service: Annotated[UserService, Depends(get_user_service)],
    current_user: AdminUser,
) -> PasswordResetResponse:
    temporary = await service.reset_password(current_user, user_id)
    return PasswordResetResponse(temporary_password=temporary)
