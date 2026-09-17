from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from corp_ed.domain.models import Track


class BriefCreateRequest(BaseModel):
    """Анкета руководителя — вход для генерации программы.

    author_id в теле нет намеренно: автор берётся из токена, иначе
    руководитель сможет записать бриф от чужого имени.
    """

    track: Track
    role_title: str = Field(min_length=1, max_length=200)
    goals: str = Field(min_length=1, max_length=2000)
    tasks: str = Field(min_length=1, max_length=2000)
    intern_level: str = Field(min_length=1, max_length=200)


class BriefResponse(BaseModel):
    """Бриф без текстовых полей: клиент их только что прислал."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    track: Track
    role_title: str
    created_at: datetime
