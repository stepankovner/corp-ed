from typing import Annotated

from fastapi import APIRouter, Depends

from corp_ed.api.v1.dependencies import get_auth_service, get_current_user
from corp_ed.api.v1.schemas.auth import LoginRequest, MeResponse, TokenResponse
from corp_ed.domain.models import User
from corp_ed.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me", response_model=MeResponse)
async def read_me(
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    return current_user


@router.post("/login", response_model=TokenResponse)
async def login(
    data: LoginRequest,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> TokenResponse:
    token = await auth_service.login(data.company_code, data.email, data.password)
    return TokenResponse(access_token=token)
