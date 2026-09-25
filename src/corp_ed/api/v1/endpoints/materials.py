from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from corp_ed.api.v1.dependencies import get_material_service, require_role
from corp_ed.api.v1.rate_limits import INGEST_PER_TENANT, limit_by_tenant
from corp_ed.api.v1.schemas.material import (
    IngestResponse,
    MaterialCreateRequest,
    MaterialResponse,
)
from corp_ed.domain.models import User, UserRole
from corp_ed.services.material_service import MaterialService

router = APIRouter(prefix="/materials", tags=["materials"])

AdminUser = Annotated[User, Depends(require_role(UserRole.ADMIN))]


@router.get("", response_model=list[MaterialResponse])
async def list_materials(
    service: Annotated[MaterialService, Depends(get_material_service)],
    current_user: AdminUser,
) -> list[MaterialResponse]:
    materials = await service.list_all()
    return [MaterialResponse.model_validate(material) for material in materials]


@router.post(
    "",
    response_model=MaterialResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(limit_by_tenant(INGEST_PER_TENANT))],
)
async def create_material(
    data: MaterialCreateRequest,
    service: Annotated[MaterialService, Depends(get_material_service)],
    current_user: AdminUser,
) -> MaterialResponse:
    """Создать материал и сразу поставить его в очередь на индексацию."""
    material = await service.create(
        current_user, title=data.title, content=data.content
    )
    return MaterialResponse.model_validate(material)


@router.get("/{material_id}", response_model=MaterialResponse)
async def get_material(
    material_id: UUID,
    service: Annotated[MaterialService, Depends(get_material_service)],
    current_user: AdminUser,
) -> MaterialResponse:
    return MaterialResponse.model_validate(await service.get(material_id))


@router.post(
    "/{material_id}/ingest",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(limit_by_tenant(INGEST_PER_TENANT))],
)
async def ingest_material(
    material_id: UUID,
    service: Annotated[MaterialService, Depends(get_material_service)],
    current_user: AdminUser,
) -> IngestResponse:
    """Переиндексировать: 202 сразу, работа — в воркере.

    Прогресс — по статусу в GET /materials/{id}.
    """
    material = await service.request_ingest(material_id)
    return IngestResponse(material_id=material.id, status=material.status)
