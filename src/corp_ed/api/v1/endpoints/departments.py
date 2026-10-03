from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from corp_ed.api.v1.dependencies import (
    get_current_user,
    get_department_service,
    require_role,
)
from corp_ed.api.v1.rate_limits import PEOPLE_EDIT_PER_TENANT, limit_by_tenant
from corp_ed.api.v1.schemas.people import DepartmentRequest, DepartmentResponse
from corp_ed.domain.models import Department, User, UserRole
from corp_ed.services.department_service import DepartmentService

router = APIRouter(prefix="/departments", tags=["departments"])

AdminUser = Annotated[User, Depends(require_role(UserRole.ADMIN))]
Member = Annotated[User, Depends(get_current_user)]
Service = Annotated[DepartmentService, Depends(get_department_service)]


def _response(department: Department, members: int) -> DepartmentResponse:
    return DepartmentResponse(id=department.id, name=department.name, members=members)


@router.get("", response_model=list[DepartmentResponse])
async def list_departments(
    service: Service, current_user: Member
) -> list[DepartmentResponse]:
    """Отделы компании — всем её людям: выбрать свой в профиле, отобрать
    коллег в справочнике (ТЗ §4)."""
    return [_response(item, count) for item, count in await service.list_with_counts()]


@router.post(
    "",
    response_model=DepartmentResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(limit_by_tenant(PEOPLE_EDIT_PER_TENANT))],
)
async def create_department(
    data: DepartmentRequest, service: Service, current_user: AdminUser
) -> DepartmentResponse:
    department = await service.create(current_user, " ".join(data.name.split()))
    return _response(department, 0)


@router.patch(
    "/{department_id}",
    response_model=DepartmentResponse,
    dependencies=[Depends(limit_by_tenant(PEOPLE_EDIT_PER_TENANT))],
)
async def rename_department(
    department_id: UUID,
    data: DepartmentRequest,
    service: Service,
    current_user: AdminUser,
) -> DepartmentResponse:
    department = await service.rename(
        current_user, department_id, " ".join(data.name.split())
    )
    counts = {item.id: count for item, count in await service.list_with_counts()}
    return _response(department, counts.get(department.id, 0))


@router.delete(
    "/{department_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(limit_by_tenant(PEOPLE_EDIT_PER_TENANT))],
)
async def delete_department(
    department_id: UUID, service: Service, current_user: AdminUser
) -> None:
    """Удалить отдел. Люди остаются, у них просто нет отдела."""
    await service.delete(current_user, department_id)
