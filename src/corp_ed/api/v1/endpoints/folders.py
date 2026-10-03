"""Папки загруженных документов с доступом по отделам (ТЗ §5, §7)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from corp_ed.api.v1.dependencies import get_folder_service, require_role
from corp_ed.api.v1.rate_limits import FOLDER_EDIT_PER_TENANT, limit_by_tenant
from corp_ed.api.v1.schemas.company import (
    DepartmentBrief,
    FolderCreateRequest,
    FolderResponse,
    FolderUpdateRequest,
)
from corp_ed.domain.models import User, UserRole
from corp_ed.services.folder_service import UNSET, FolderService, FolderView

router = APIRouter(prefix="/folders", tags=["materials"])

Admin = Annotated[User, Depends(require_role(UserRole.ADMIN))]
Service = Annotated[FolderService, Depends(get_folder_service)]
EDIT_LIMIT = [Depends(limit_by_tenant(FOLDER_EDIT_PER_TENANT))]


def _response(view: FolderView) -> FolderResponse:
    return FolderResponse(
        id=view.folder.id,
        name=view.folder.name,
        restricted=view.folder.restricted,
        departments=[DepartmentBrief(id=d.id, name=d.name) for d in view.departments],
        documents=view.documents,
        created_at=view.folder.created_at,
    )


@router.get("", response_model=list[FolderResponse])
async def list_folders(service: Service, admin: Admin) -> list[FolderResponse]:
    return [_response(view) for view in await service.all()]


@router.post(
    "",
    response_model=FolderResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=EDIT_LIMIT,
)
async def create_folder(
    data: FolderCreateRequest, service: Service, admin: Admin
) -> FolderResponse:
    """restricted — только отделы department_ids и администраторы."""
    view = await service.create(
        admin,
        name=data.name,
        restricted=data.restricted,
        department_ids=data.department_ids,
    )
    return _response(view)


@router.patch("/{folder_id}", response_model=FolderResponse, dependencies=EDIT_LIMIT)
async def update_folder(
    folder_id: UUID, data: FolderUpdateRequest, service: Service, admin: Admin
) -> FolderResponse:
    view = await service.update(
        admin,
        folder_id,
        name=data.name if data.name is not None else UNSET,
        restricted=data.restricted if data.restricted is not None else UNSET,
        department_ids=data.department_ids
        if data.department_ids is not None
        else UNSET,
    )
    return _response(view)


@router.delete(
    "/{folder_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=EDIT_LIMIT
)
async def delete_folder(folder_id: UUID, service: Service, admin: Admin) -> None:
    """Только пустую: иначе документы закрытой папки стали бы видны всем."""
    await service.delete(admin, folder_id)
