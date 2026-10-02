from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from corp_ed.api.v1.schemas.base import RequestModel
from corp_ed.domain.models import MaterialStatus

# Текст, вставленный в форму. Файлы — через /materials/upload со своим
# лимитом. Ингест фоновый, поэтому лимит — про размер тела запроса и
# стоимость эмбеддингов, а не про время HTTP-запроса.
MAX_MATERIAL_LENGTH = 200_000
MAX_TITLE_LENGTH = 200

# Название из одних пробелов — не название (стенд 02.10: принималось).
Title = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TITLE_LENGTH),
]


class MaterialCreateRequest(RequestModel):
    """Документ компании в виде текста (Markdown или простой текст).

    title — человеческое название («Положение об отпусках»), а не имя
    файла: оно уходит в крошки эмбеддинга и в подписи источников.
    """

    title: Title
    content: str = Field(min_length=1, max_length=MAX_MATERIAL_LENGTH)

    @field_validator("content")
    @classmethod
    def _has_text(cls, value: str) -> str:
        # Из одних пробелов индексировать нечего, а статус был бы «готов».
        if not value.strip():
            raise ValueError("Текст документа пустой")
        return value


class MaterialUpdateRequest(RequestModel):
    title: Title


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
    # Документ из источника: подключение, ссылка, когда синхронизирован.
    connector_id: UUID | None = None
    source_url: str | None = None
    synced_at: datetime | None = None
    visibility: str = "tenant"
    created_at: datetime


class IngestResponse(BaseModel):
    material_id: UUID
    status: MaterialStatus
