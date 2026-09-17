from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from corp_ed.domain.models import Track


@dataclass(frozen=True)
class ChunkMatch:
    id: UUID
    content: str
    material_id: UUID
    position: int
    distance: float


@dataclass(frozen=True)
class FaqAnswer:
    content: str
    answer_given: bool
    sources: list[ChunkMatch]


@dataclass(frozen=True)
class MaterialSummary:
    """Материал в списке: без текста, но с числом проиндексированных чанков.

    Число чанков живёт в другой таблице, поэтому сущность Material его
    не несёт — сводку собирает сервис.
    """

    id: UUID
    track: Track
    title: str
    created_at: datetime
    chunks: int
