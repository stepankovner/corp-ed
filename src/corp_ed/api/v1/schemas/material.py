from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from corp_ed.api.v1.schemas.base import RequestModel
from corp_ed.domain.models import MaterialStatus

# Текст, вставленный в форму. Файлы — через /materials/upload со своим
# лимитом. Ингест фоновый, поэтому лимит — про размер тела запроса и
# стоимость эмбеддингов, а не про время HTTP-запроса.
MAX_MATERIAL_LENGTH = 200_000
MAX_TITLE_LENGTH = 200


class MaterialCreateRequest(RequestModel):
    """Документ компании в виде текста (Markdown или простой текст).

    title — человеческое название («Положение об отпусках»), а не имя
    файла: оно уходит в крошки эмбеддинга и в подписи источников.
    """

    title: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)
    content: str = Field(min_length=1, max_length=MAX_MATERIAL_LENGTH)


class MaterialUpdateRequest(RequestModel):
    title: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH)


class MaterialResponse(BaseModel):
    """Материал без содержимого: клиенту нужен статус, а не текст."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    source_filename: str | None
    source_format: str | None
    source_size: int | None
    status: MaterialStatus
    status_error: str | None
    indexed_at: datetime | None
    created_at: datetime


class IngestResponse(BaseModel):
    material_id: UUID
    status: MaterialStatus
