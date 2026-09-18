from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from corp_ed.domain.models import ProgramStatus


class ProgramGenerateRequest(BaseModel):
    """Данные для генерации программы (входящий запрос)"""

    brief_id: UUID


class ProgramUpdateRequest(BaseModel):
    """Правка черновика: текст и/или назначенный стажёр.

    Оба поля необязательные: экран сохраняет то, что руководитель менял.
    """

    content: str | None = Field(default=None, min_length=1)
    intern_id: UUID | None = None


class ProgramResponse(BaseModel):
    """Идентификатор и статус программы после генерации"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: ProgramStatus


class ProgramDetailResponse(BaseModel):
    """Полные данные программы"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: ProgramStatus
    content: str
    created_at: datetime
    intern_id: UUID | None
    role_title: str


class ProgramListItemResponse(BaseModel):
    """Программа в списке: без содержимого, оно приходит отдельной ручкой."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: ProgramStatus
    role_title: str
    created_at: datetime
