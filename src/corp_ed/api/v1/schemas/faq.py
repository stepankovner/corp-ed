from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from corp_ed.api.v1.schemas.base import RequestModel
from corp_ed.domain.types import AnswerOrigin


class FaqQuestionRequest(RequestModel):
    question: str = Field(min_length=1, max_length=1000)


class FaqSourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    material_id: UUID
    title: str
    heading_path: list[str]
    position: int
    content: str


class FaqAnswerResponse(BaseModel):
    """Ответ ассистента.

    origin = general_knowledge — в документах компании ответа нет, ответ
    из общих знаний: content начинается с «В документах компании ответа
    нет. Общая информация:», sources пуст. Фронт обязан показать это
    явно (плашка), а не только текстом.
    """

    model_config = ConfigDict(from_attributes=True)

    content: str
    answer_given: bool
    origin: AnswerOrigin
    sources: list[FaqSourceResponse]
