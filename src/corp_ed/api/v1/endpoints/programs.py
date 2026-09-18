from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from corp_ed.api.v1.dependencies import (
    get_current_user,
    get_program_service,
    require_role,
)
from corp_ed.api.v1.schemas.program import (
    ProgramDetailResponse,
    ProgramGenerateRequest,
    ProgramListItemResponse,
    ProgramResponse,
    ProgramUpdateRequest,
)
from corp_ed.domain.models import Program, User, UserRole
from corp_ed.services.program_service import ProgramService

router = APIRouter(prefix="/programs", tags=["programs"])


def _detail(program: Program) -> ProgramDetailResponse:
    """Собрать ответ: должность лежит в брифе, а не в самой программе."""
    return ProgramDetailResponse(
        id=program.id,
        status=program.status,
        content=program.content,
        created_at=program.created_at,
        intern_id=program.intern_id,
        role_title=program.brief.role_title,
    )


@router.get("", response_model=list[ProgramListItemResponse])
async def list_programs(
    service: Annotated[ProgramService, Depends(get_program_service)],
    current_user: Annotated[User, Depends(require_role(UserRole.MANAGER))],
) -> list[ProgramListItemResponse]:
    programs = await service.list_all()
    return [ProgramListItemResponse.model_validate(program) for program in programs]


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


# Объявлено выше "/{program_id}": иначе путь "my" попадёт в него как UUID
# и запрос стажёра закончится ошибкой разбора параметра.
@router.get("/my", response_model=ProgramDetailResponse)
async def get_my_program(
    service: Annotated[ProgramService, Depends(get_program_service)],
    current_user: Annotated[User, Depends(require_role(UserRole.INTERN))],
) -> ProgramDetailResponse:
    program = await service.get_for_intern(current_user)
    return _detail(program)


@router.get("/{program_id}", response_model=ProgramDetailResponse)
async def get_program(
    program_id: UUID,
    service: Annotated[ProgramService, Depends(get_program_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> ProgramDetailResponse:
    program = await service.get(program_id, current_user)
    return _detail(program)


@router.patch("/{program_id}", response_model=ProgramDetailResponse)
async def update_program(
    program_id: UUID,
    data: ProgramUpdateRequest,
    service: Annotated[ProgramService, Depends(get_program_service)],
    current_user: Annotated[User, Depends(require_role(UserRole.MANAGER))],
) -> ProgramDetailResponse:
    program = await service.update(
        program_id,
        content=data.content,
        intern_id=data.intern_id,
    )
    return _detail(program)


@router.post("/{program_id}/approve", response_model=ProgramDetailResponse)
async def approve_program(
    program_id: UUID,
    service: Annotated[ProgramService, Depends(get_program_service)],
    current_user: Annotated[User, Depends(require_role(UserRole.MANAGER))],
) -> ProgramDetailResponse:
    program = await service.approve(program_id)
    return _detail(program)
