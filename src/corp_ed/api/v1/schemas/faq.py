from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from corp_ed.api.v1.schemas.base import RequestModel
from corp_ed.domain.types import AnswerOrigin

MAX_QUESTION_LENGTH = 1000
MAX_SEARCH_LIMIT = 50


class FaqQuestionRequest(RequestModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)


class FaqSourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    material_id: UUID
    title: str
    heading_path: list[str]
    position: int
    content: str


class AnswerDiagnosticsResponse(BaseModel):
    """Только для ADMIN: модель, версия промпта, токены — для eval (E5)."""

    model_config = ConfigDict(from_attributes=True)

    model: str
    prompt_version: str
    input_tokens: int
    output_tokens: int
    credits: int
    nearest_distance: float | None


class FaqAnswerResponse(BaseModel):
    """Ответ ассистента.

    origin = general_knowledge — в документах компании ответа нет, ответ
    из общих знаний: content начинается с «В документах компании ответа
    нет. Общая информация:», sources пуст. Фронт обязан показать это
    явно (плашка), а не только текстом.

    answer_id — для оценки 👍/👎 (PATCH /faq/answers/{answer_id}).
    """

    model_config = ConfigDict(from_attributes=True)

    answer_id: UUID | None
    content: str
    answer_given: bool
    origin: AnswerOrigin
    sources: list[FaqSourceResponse]
    diagnostics: AnswerDiagnosticsResponse | None = None


class FaqSearchRequest(RequestModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)
    limit: int = Field(default=10, ge=1, le=MAX_SEARCH_LIMIT)


class FaqSearchMatch(BaseModel):
    """Форма ответа согласована с клиентом eval (ml-backend-contracts, 3)."""

    chunk_id: UUID
    material_id: UUID
    material_title: str
    position: int
    heading_path: list[str]
    content: str
    distance: float


class FaqSearchResponse(BaseModel):
    matches: list[FaqSearchMatch]


class FeedbackRequest(RequestModel):
    value: Literal[-1, 1]
