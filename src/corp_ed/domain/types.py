from dataclasses import dataclass
from uuid import UUID


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
