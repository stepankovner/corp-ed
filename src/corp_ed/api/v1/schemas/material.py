from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from corp_ed.api.v1.schemas.base import RequestModel

# Ингест синхронный: ~0.7 с на чанк. 20 000 символов при chunk_tokens=400
# (≈1200 символов тела) дают ~17 чанков ≈ 12 с. Поднимать только вместе
# с переездом ингеста в фоновые задачи.
MAX_MATERIAL_LENGTH = 20_000


class MaterialCreateRequest(RequestModel):
    """Документ компании в виде текста (Markdown или простой текст)."""

    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=MAX_MATERIAL_LENGTH)


class MaterialResponse(BaseModel):
    """Материал без содержимого: клиент его только что прислал."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    created_at: datetime


class IngestResponse(BaseModel):
    material_id: UUID
    chunks: int
