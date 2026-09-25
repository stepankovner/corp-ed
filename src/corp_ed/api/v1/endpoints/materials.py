from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from corp_ed.api.v1.dependencies import get_material_service, require_role
from corp_ed.api.v1.schemas.material import (
    IngestResponse,
    MaterialCreateRequest,
    MaterialResponse,
)
from corp_ed.domain.models import User, UserRole
from corp_ed.services.material_service import MaterialService

router = APIRouter(prefix="/materials", tags=["materials"])


@router.post(
    "",
    response_model=MaterialResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_material(
    data: MaterialCreateRequest,
    service: Annotated[MaterialService, Depends(get_material_service)],
    current_user: Annotated[User, Depends(require_role(UserRole.ADMIN))],
) -> MaterialResponse:
    material = await service.create(
        current_user, title=data.title, content=data.content
    )
    return MaterialResponse.model_validate(material)


@router.post("/{material_id}/ingest", response_model=IngestResponse)
async def ingest_material(
    material_id: UUID,
    service: Annotated[MaterialService, Depends(get_material_service)],
    current_user: Annotated[User, Depends(require_role(UserRole.ADMIN))],
) -> IngestResponse:
    length = await service.ingest(material_id=material_id)
    return IngestResponse(material_id=material_id, chunks=length)
