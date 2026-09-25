from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class ChunkMatch:
    """Найденный чанк в том виде, в каком его видит сервис ответа.

    content — llm_text чанка (крошки + Markdown), именно он уходит в
    промпт. title и heading_path нужны промпту v2, чтобы подписать
    выдержку источником «[1] Документ > Раздел», и фронту — чтобы
    показать ссылку. Поля совпадают с Protocol SourceChunk из
    prompts/faq.py: наследоваться от него не нужно.
    """

    id: UUID
    content: str
    material_id: UUID
    position: int
    distance: float
    title: str
    heading_path: list[str]


@dataclass(frozen=True)
class FaqAnswer:
    content: str
    answer_given: bool
    sources: list[ChunkMatch]
