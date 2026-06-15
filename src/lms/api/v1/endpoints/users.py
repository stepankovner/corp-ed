from typing import Annotated

from fastapi import APIRouter, Depends, status

from lms.api.v1.dependencies import get_user_service
from lms.api.v1.schemas.user import UserCreate, UserResponse
from lms.services.user_service import UserService

router = APIRouter(prefix="/users", tags=["users"])


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
