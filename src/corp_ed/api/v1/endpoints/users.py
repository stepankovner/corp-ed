from typing import Annotated

from fastapi import APIRouter, Depends, status

from corp_ed.api.v1.dependencies import get_user_service, require_role
from corp_ed.api.v1.schemas.user import InternResponse, UserCreate, UserResponse
from corp_ed.domain.models import User, UserRole
from corp_ed.services.user_service import UserService

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/interns", response_model=list[InternResponse])
async def list_interns(
    service: Annotated[UserService, Depends(get_user_service)],
    current_user: Annotated[User, Depends(require_role(UserRole.MANAGER))],
) -> list[InternResponse]:
    interns = await service.list_interns()
    return [InternResponse.model_validate(intern) for intern in interns]


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_user(
    data: UserCreate,
    service: Annotated[UserService, Depends(get_user_service)],
) -> UserResponse:
    user = await service.register(data)
    return UserResponse.model_validate(user)
