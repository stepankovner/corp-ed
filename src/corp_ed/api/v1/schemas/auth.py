from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr

from corp_ed.domain.models import UserRole


class LoginRequest(BaseModel):
    company_code: str
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    full_name: str | None
    role: UserRole
    tenant_id: UUID
