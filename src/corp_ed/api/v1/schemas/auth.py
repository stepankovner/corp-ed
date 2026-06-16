from pydantic import BaseModel, EmailStr


class LoginRequest(BaseModel):
    company_code: str
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
