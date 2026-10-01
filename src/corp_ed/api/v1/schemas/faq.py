from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from corp_ed.api.v1.schemas.base import RequestModel
from corp_ed.domain.types import AnswerOrigin, Retriever

MAX_QUESTION_LENGTH = 1000
MAX_SEARCH_LIMIT = 50


class FaqQuestionRequest(RequestModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)
    # Диалог, который продолжает сотрудник (BH-28): conversation_id из
    # прошлого ответа. Нет — новый диалог. Чужой или истёкший id не
    # ошибка: истории просто нет (ключ хранилища включает сотрудника).
    conversation_id: UUID | None = None


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
    # Память диалога (BH-28): как понят вопрос после переписывания и
    # сколько прошлых реплик учтено — для замера ML на стенде.
    standalone_question: str | None = None
    history_turns: int = 0
    # Реранкер (M3): модель, если порядок выдержек дал он, и время.
    rerank_model: str | None = None
    rerank_ms: int | None = None


class FaqAnswerResponse(BaseModel):
    """Ответ ассистента.

    origin:
    - documents — ответ по документам компании, sources — выдержки;
    - general_knowledge — в документах ответа нет, ответ из общих
      знаний: content начинается с GENERAL_ANSWER_PREFIX («В документах
      компании ответа нет. Ниже — общая информация, не из документов
      компании:»), sources пуст. Фронт обязан показать это явно
      (плашка), а не только текстом;
    - none — в документах ответа нет, компания в строгом режиме, или
      провайдер отфильтровал ответ: content начинается с
      NOT_FOUND_ANSWER («В документах компании ответа нет.»), дальше —
      совет уточнить у руководителя или в профильном отделе; sources пуст.

    answer_id — для оценки 👍/👎 (PATCH /faq/answers/{answer_id}).
    conversation_id — диалог (BH-28): прислать со следующим вопросом,
    чтобы уточняющий вопрос понимался в контексте; «Новый диалог» — не
    присылать. Номера [n] относятся только к sources этого ответа.
    """

    model_config = ConfigDict(from_attributes=True)

    answer_id: UUID | None
    content: str
    answer_given: bool
    origin: AnswerOrigin
    sources: list[FaqSourceResponse]
    diagnostics: AnswerDiagnosticsResponse | None = None
    conversation_id: UUID | None = None


class FaqSearchRequest(RequestModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)
    limit: int = Field(default=10, ge=1, le=MAX_SEARCH_LIMIT)
    # Сравнить способы поиска на живой базе, не меняя RAG_RETRIEVER.
    # Не задан — как в /faq/ask.
    retriever: Retriever | None = None
    # Порядок, который дал бы ответ с реранкером (BH-32), и балл; 409 —
    # реранкер выключен (RAG_RERANK_MODEL пуст) или поиск не векторный,
    # 503 — реранкер не ответил.
    rerank: bool = False


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
    rerank_score: float | None = None


class FaqSearchResponse(BaseModel):
    matches: list[FaqSearchMatch]


class FeedbackRequest(RequestModel):
    value: Literal[-1, 1]
