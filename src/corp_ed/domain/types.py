from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class ChunkMatch:
    id: UUID
    content: str
    material_id: UUID
    position: int
    distance: float
