from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class CourseCreate(BaseModel):
    title: str


class CourseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    created_at: datetime
    # tenant_id наружу НЕ отдаём — внутренняя деталь изоляции
