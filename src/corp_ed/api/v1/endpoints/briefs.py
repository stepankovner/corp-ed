from typing import Annotated

from fastapi import APIRouter, Depends, status

from corp_ed.api.v1.dependencies import get_brief_service, require_role
from corp_ed.api.v1.schemas.brief import BriefCreateRequest, BriefResponse
from corp_ed.domain.models import User, UserRole
from corp_ed.services.brief_service import BriefService

router = APIRouter(prefix="/briefs", tags=["briefs"])


@router.post(
    "",
    response_model=BriefResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_brief(
    data: BriefCreateRequest,
    service: Annotated[BriefService, Depends(get_brief_service)],
    current_user: Annotated[User, Depends(require_role(UserRole.MANAGER))],
) -> BriefResponse:
    brief = await service.create(
        author_id=current_user.id,
        track=data.track,
        role_title=data.role_title,
        goals=data.goals,
        tasks=data.tasks,
        intern_level=data.intern_level,
    )
    return BriefResponse.model_validate(brief)
