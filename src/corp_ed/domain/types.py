from dataclasses import dataclass
from enum import StrEnum
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


class AnswerOrigin(StrEnum):
    """Откуда взят ответ.

    DOCUMENTS — из выдержек документов компании, со ссылками.
    GENERAL_KNOWLEDGE — в документах ответа не нашлось, модель ответила
    из общих знаний. Такой ответ всегда помечен: первой строкой текста
    (GENERAL_ANSWER_PREFIX) и этим полем — фронт показывает предупреждение
    по полю, не разбирая текст.
    """

    DOCUMENTS = "documents"
    GENERAL_KNOWLEDGE = "general_knowledge"


@dataclass(frozen=True)
class FaqAnswer:
    content: str
    answer_given: bool
    """Ответ дан ПО ДОКУМЕНТАМ. Для общего ответа — False: для журнала
    ответов и отчёта о пробелах это вопрос, на который в базе знаний
    ответа нет."""
    origin: AnswerOrigin
    sources: list[ChunkMatch]
