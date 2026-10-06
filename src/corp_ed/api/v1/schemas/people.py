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
    department_confirmed: bool
    """Отдел подтверждён администратором (ТЗ §7): только тогда человеку
    открыты закрытые папки отдела. Выбранный самим — false до
    подтверждения; отдела нет — false."""
    role: UserRole


class PersonUpdateRequest(RequestModel):
    """Должность и отдел в компании. Пришедшее null — очистить.

    Отдел (ТЗ §7): назначенный администратором подтверждён сразу;
    выбранный самим сотрудником ждёт подтверждения (department_confirmed
    false), а тот же, что уже стоит, ничего не меняет. null — снять отдел
    сразу."""

    position: ProfileText | None = None
    department_id: UUID | None = None


class DepartmentResponse(BaseModel):
    id: UUID
    name: str
    members: int
    """Сколько работающих людей выбрали этот отдел."""
    unconfirmed: int
    """Из них ждут подтверждения отдела администратором (ТЗ §7): закрытые
    папки отдела им не открыты."""


class DepartmentRequest(RequestModel):
    name: str = Field(min_length=1, max_length=MAX_DEPARTMENT_NAME, pattern=r"\S")
