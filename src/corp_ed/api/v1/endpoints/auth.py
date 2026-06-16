from typing import Annotated

from fastapi import APIRouter, Depends

from corp_ed.api.v1.dependencies import get_auth_service
from corp_ed.api.v1.schemas.auth import LoginRequest, TokenResponse
from corp_ed.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(
    data: LoginRequest,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> TokenResponse:
    token = await auth_service.login(data.company_code, data.email, data.password)
    return TokenResponse(access_token=token)
