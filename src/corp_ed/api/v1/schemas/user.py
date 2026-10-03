from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr

from corp_ed.api.v1.schemas.base import RequestModel
from corp_ed.domain.models import MemberStatus, UserRole


class UserUpdateRequest(RequestModel):
    role: UserRole | None = None
    blocked: bool | None = None
    """true — заблокировать, false — разблокировать."""


class UserResponse(BaseModel):
    """Человек в компании: членство и имя с почтой из учётки."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr | None
    """null — человек удалил учётку."""
    full_name: str | None
    role: UserRole
    status: MemberStatus
    last_login_at: datetime | None
    created_at: datetime
