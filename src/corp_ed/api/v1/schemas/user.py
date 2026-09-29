from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from corp_ed.api.v1.schemas.base import RequestModel
from corp_ed.core.password_policy import MAX_PASSWORD_LENGTH
from corp_ed.domain.models import UserRole


class UserCreateRequest(RequestModel):
    """Новый сотрудник. Без password система сгенерирует временный."""

    email: EmailStr
    full_name: str | None = Field(default=None, max_length=200)
    role: UserRole = UserRole.EMPLOYEE
    password: str | None = Field(default=None, max_length=MAX_PASSWORD_LENGTH)


class UserUpdateRequest(RequestModel):
    role: UserRole | None = None
    is_active: bool | None = None
    full_name: str | None = Field(default=None, max_length=200)


class UserResponse(BaseModel):
    """Пользователь без пароля, хеша и версии токенов."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    full_name: str | None
    role: UserRole
    is_active: bool
    must_change_password: bool
    last_login_at: datetime | None
    created_at: datetime


class UserCreatedResponse(BaseModel):
    user: UserResponse
    temporary_password: str | None
    """Показывается один раз. Если пароль задал администратор — null."""


class PasswordResetResponse(BaseModel):
    temporary_password: str
