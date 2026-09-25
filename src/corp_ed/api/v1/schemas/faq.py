from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from corp_ed.api.v1.schemas.base import RequestModel
from corp_ed.domain.types import AnswerOrigin, Retriever

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
    # Ссылка на документ в источнике (коннекторы); у загрузок пусто.
    source_url: str | None = None


class AnswerDiagnosticsResponse(BaseModel):
    """Только для ADMIN: модель, версия промпта, токены — для eval (E5)."""

    model_config = ConfigDict(from_attributes=True)

    model: str | None
    model_version: str | None
    prompt_version: str
    input_tokens: int
    output_tokens: int
    credits: int
    nearest_distance: float | None


class FaqAnswerResponse(BaseModel):
    """Ответ ассистента.

    origin:
    - documents — ответ по документам компании, sources — выдержки;
    - general_knowledge — в документах ответа нет, ответ из общих
      знаний: content начинается с GENERAL_ANSWER_PREFIX («В документах
      компании ответа нет. Ниже — общая информация, не из документов
      компании:»), sources пуст. Фронт обязан показать это явно
      (плашка), а не только текстом;
    - none — в документах ответа нет, компания в строгом режиме:
      content = NOT_FOUND_ANSWER, sources пуст.

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
    # Сравнить способы поиска на живой базе, не меняя RAG_RETRIEVER.
    # Не задан — как в /faq/ask.
    retriever: Retriever | None = None


class FaqSearchMatch(BaseModel):
    """Форма ответа согласована с клиентом eval (ml-backend-contracts, 3)."""

    chunk_id: UUID
    material_id: UUID
    material_title: str
    position: int
    heading_path: list[str]
    content: str
    distance: float
    fulltext_rank: float | None


class FaqSearchResponse(BaseModel):
    matches: list[FaqSearchMatch]


class FeedbackRequest(RequestModel):
    value: Literal[-1, 1]
