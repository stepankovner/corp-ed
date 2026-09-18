from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from corp_ed.domain.models import ProgramStatus


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
class ProgramSummary:
    """Программа в списке: должность берётся из брифа, содержимое не нужно."""

    id: UUID
    status: ProgramStatus
    role_title: str
    created_at: datetime
