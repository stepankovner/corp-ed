from uuid import UUID

from pydantic import BaseModel, ConfigDict

from corp_ed.domain.models import ProgramStatus


class ProgramGenerateRequest(BaseModel):
    """Данные для генерации программы (входящий запрос)."""

    brief_id: UUID


class ProgramResponse(BaseModel):
    """Данные программы для ответа."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: ProgramStatus
