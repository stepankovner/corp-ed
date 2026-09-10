from typing import Annotated

from fastapi import APIRouter, Depends, status

from corp_ed.api.v1.dependencies import (
    get_program_service,
    require_role,
)
from corp_ed.api.v1.schemas.program import ProgramGenerateRequest, ProgramResponse
from corp_ed.domain.models import User, UserRole
from corp_ed.services.program_service import ProgramService

router = APIRouter(prefix="/programs", tags=["programs"])


@router.post(
    "/generate",
    response_model=ProgramResponse,
    status_code=status.HTTP_201_CREATED,
)
async def generate_program(
    data: ProgramGenerateRequest,
    service: Annotated[ProgramService, Depends(get_program_service)],
    current_user: Annotated[User, Depends(require_role(UserRole.MANAGER))],
) -> ProgramResponse:
    program = await service.generate(data.brief_id)
    return ProgramResponse.model_validate(program)
