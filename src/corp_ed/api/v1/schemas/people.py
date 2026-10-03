from uuid import UUID

from pydantic import BaseModel, Field

from corp_ed.api.v1.schemas.auth import DepartmentRef, ProfileText
from corp_ed.api.v1.schemas.base import RequestModel
from corp_ed.domain.models import UserRole

MAX_DEPARTMENT_NAME = 100


class PersonResponse(BaseModel):
    """Коллега в справочнике компании (ТЗ §4)."""

    member_id: UUID
    first_name: str | None
    last_name: str | None
    patronymic: str | None
    full_name: str | None
    email: str
    phone: str | None
    telegram: str | None
    avatar_url: str | None
    position: str | None
    department: DepartmentRef | None
    role: UserRole


class PersonUpdateRequest(RequestModel):
    """Должность и отдел в компании. Пришедшее null — очистить."""

    position: ProfileText | None = None
    department_id: UUID | None = None


class DepartmentResponse(BaseModel):
    id: UUID
    name: str
    members: int
    """Сколько работающих людей выбрали этот отдел."""


class DepartmentRequest(RequestModel):
    name: str = Field(min_length=1, max_length=MAX_DEPARTMENT_NAME, pattern=r"\S")
