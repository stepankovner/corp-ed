from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from corp_ed.domain.models import ProgramStatus


class ProgramGenerateRequest(BaseModel):
    """Данные для генерации программы (входящий запрос)"""

    brief_id: UUID


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
    brief_id: UUID
    content: str
    created_at: datetime
