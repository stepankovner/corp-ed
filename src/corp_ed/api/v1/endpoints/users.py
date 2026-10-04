from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from corp_ed.api.v1.dependencies import get_user_service, require_role
from corp_ed.api.v1.schemas.user import UserResponse, UserUpdateRequest
from corp_ed.domain.models import User, UserRole
from corp_ed.services.user_service import UserService

router = APIRouter(prefix="/users", tags=["users"])

AdminUser = Annotated[User, Depends(require_role(UserRole.ADMIN))]
Service = Annotated[UserService, Depends(get_user_service)]


@router.get("", response_model=list[UserResponse])
async def list_users(service: Service, current_user: AdminUser) -> list[UserResponse]:
    """Люди компании: работают, заблокированы, ждут одобрения."""
    return [UserResponse.model_validate(user) for user in await service.list_users()]


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: UUID, data: UserUpdateRequest, service: Service, current_user: AdminUser
) -> UserResponse:
    user = await service.update_user(
        current_user, user_id, role=data.role, blocked=data.blocked
    )
    return UserResponse.model_validate(user)


@router.post("/{user_id}/approve", response_model=UserResponse)
async def approve_user(
    user_id: UUID, service: Service, current_user: AdminUser
) -> UserResponse:
    """Пустить вступившего по приглашению с одобрением."""
    return UserResponse.model_validate(await service.approve(current_user, user_id))


@router.post("/{user_id}/reject", status_code=status.HTTP_204_NO_CONTENT)
async def reject_user(user_id: UUID, service: Service, current_user: AdminUser) -> None:
    await service.reject(current_user, user_id)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_user(user_id: UUID, service: Service, current_user: AdminUser) -> None:
    """Убрать из компании. Учётка человека остаётся."""
    await service.remove(current_user, user_id)
