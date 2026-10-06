from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from corp_ed.api.v1.dependencies import get_user_service, require_role
from corp_ed.api.v1.rate_limits import PEOPLE_EDIT_PER_TENANT, limit_by_tenant
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


@router.post(
    "/{user_id}/department/confirm",
    response_model=UserResponse,
    dependencies=[Depends(limit_by_tenant(PEOPLE_EDIT_PER_TENANT))],
)
async def confirm_department(
    user_id: UUID, service: Service, current_user: AdminUser
) -> UserResponse:
    """Подтвердить отдел, который сотрудник выбрал сам (ТЗ §7): с этого
    момента ему открыты закрытые папки отдела. Уже подтверждён — ответ
    тот же, без изменений. Отдела нет — 409. Только работающий человек
    своей компании, иначе 404. Человеку — уведомление, в журнал."""
    user = await service.confirm_department(current_user, user_id)
    return UserResponse.model_validate(user)


@router.post(
    "/{user_id}/department/reject",
    response_model=UserResponse,
    dependencies=[Depends(limit_by_tenant(PEOPLE_EDIT_PER_TENANT))],
)
async def reject_department(
    user_id: UUID, service: Service, current_user: AdminUser
) -> UserResponse:
    """Отклонить отдел, выбранный сотрудником (ТЗ §7): отдел у человека
    снимается (department_id null). Отдела нет или он уже подтверждён —
    409 (подтверждённый меняют в профиле сотрудника). Только работающий
    человек своей компании, иначе 404. Человеку — уведомление, в журнал."""
    user = await service.reject_department(current_user, user_id)
    return UserResponse.model_validate(user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_user(user_id: UUID, service: Service, current_user: AdminUser) -> None:
    """Убрать из компании. Учётка человека остаётся."""
    await service.remove(current_user, user_id)
