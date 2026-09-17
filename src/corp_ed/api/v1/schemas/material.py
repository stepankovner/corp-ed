from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from corp_ed.domain.models import Track

# Ингест синхронный: ~0.7 с на чанк. 20 000 символов при chunk_size=1000
# дают ~20 чанков ≈ 15 с, при chunk_size=500 — ≈ 30 с. Поднимать только
# вместе с переездом ингеста в фоновые задачи.
MAX_MATERIAL_LENGTH = 20_000


class MaterialCreateRequest(BaseModel):
    """Материал отдела в виде текста (загрузка файлов — позже)."""

    track: Track
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=MAX_MATERIAL_LENGTH)


class MaterialResponse(BaseModel):
    """Материал без содержимого: клиент его только что прислал."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    track: Track
    title: str
    created_at: datetime


class IngestResponse(BaseModel):
    material_id: UUID
    chunks: int
