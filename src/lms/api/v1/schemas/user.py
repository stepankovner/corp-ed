from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr


class UserCreate(BaseModel):
    """Данные для регистрации пользователя (входящий запрос)."""

    email: EmailStr
    password: str
    full_name: str | None = None


class UserResponse(BaseModel):
    """Данные пользователя для ответа (без пароля)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    full_name: str | None
    is_active: bool
    created_at: datetime
