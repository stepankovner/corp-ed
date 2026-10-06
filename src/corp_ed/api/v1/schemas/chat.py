"""Схемы чата (ТЗ §6): диалоги, сообщения, поток ответа, вложения, подсказки."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, RootModel

from corp_ed.api.v1.schemas.base import RequestModel
from corp_ed.api.v1.schemas.faq import AnswerDiagnosticsResponse
from corp_ed.domain.models import ChatAttachment, Conversation
from corp_ed.domain.types import AnswerOrigin
from corp_ed.services.chat_service import (
    MAX_FEEDBACK_COMMENT,
    MAX_QUESTION_CHARS,
    MAX_TITLE_CHARS,
    MessageView,
)
from corp_ed.services.suggestion_service import MAX_TEXT as MAX_SUGGESTION_TEXT

FeedbackReason = Literal[
    "inaccurate", "incomplete", "outdated", "wrong_source", "other"
]


class ConversationSummary(BaseModel):
    id: UUID
    title: str
    pinned: bool
    shared: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, conversation: Conversation) -> "ConversationSummary":
        return cls(
            id=conversation.id,
            title=conversation.title,
            pinned=conversation.pinned_at is not None,
            shared=conversation.share_token is not None,
            created_at=conversation.created_at,
            updated_at=conversation.updated_at,
        )


class ConversationListResponse(BaseModel):
    """Закреплённые — первыми (только на первой странице), затем по
    активности. next_before — передать в before за следующей страницей."""

    items: list[ConversationSummary]
    next_before: datetime | None


class AttachmentResponse(BaseModel):
    id: UUID
    filename: str
    size: int
    tokens: int

    @classmethod
    def of(cls, attachment: ChatAttachment) -> "AttachmentResponse":
        return cls(
            id=attachment.id,
            filename=attachment.filename,
            size=attachment.size,
            tokens=attachment.tokens,
        )


class MessageSourceResponse(BaseModel):
    """Выдержка ответа. kind=attachment — из вложения сотрудника.
    content=null — документ удалён или недоступен смотрящему."""

    kind: Literal["document", "attachment"]
    title: str
    heading_path: list[str]
    position: int
    content: str | None
    source_url: str | None
    material_id: UUID | None
    attachment_id: UUID | None


class MessageResponse(BaseModel):
    """Вопрос или ответ.

    status: complete; generating — ответ ещё пишется (поток или фоновая
    задача; обновите диалог позже); stopped — остановлен сотрудником,
    content — что успело прийти; failed — не удалось (error_code:
    credits_exhausted, busy, llm_unavailable, timeout, interrupted, internal).
    siblings — версии этого сообщения по порядку, включая его само
    (правки вопроса, «Ответить заново»); переключить — PUT …/current.
    origin, sources и оценка — только у ответа.
    """

    id: UUID
    parent_id: UUID | None
    role: Literal["user", "assistant"]
    content: str
    status: Literal["complete", "generating", "stopped", "failed"]
    origin: AnswerOrigin | None
    sources: list[MessageSourceResponse]
    attachments: list[AttachmentResponse]
    error_code: str | None
    feedback: Literal[-1, 1] | None
    feedback_reason: FeedbackReason | None
    feedback_comment: str | None
    siblings: list[UUID]
    created_at: datetime

    @classmethod
    def of(cls, view: MessageView, *, with_feedback: bool = True) -> "MessageResponse":
        message = view.message
        return cls(
            id=message.id,
            parent_id=message.parent_id,
            role=message.role,  # type: ignore[arg-type]
            content=view.content if view.content is not None else message.content,
            status=message.status,  # type: ignore[arg-type]
            origin=AnswerOrigin(message.origin) if message.origin else None,
            sources=[
                MessageSourceResponse(
                    kind=source.kind,  # type: ignore[arg-type]
                    title=source.title,
                    heading_path=source.heading_path,
                    position=source.position,
                    content=source.content,
                    source_url=source.source_url,
                    material_id=source.material_id,
                    attachment_id=source.attachment_id,
                )
                for source in view.sources
            ],
            attachments=[AttachmentResponse.of(a) for a in view.attachments],
            error_code=message.error_code,
            feedback=message.feedback if with_feedback else None,  # type: ignore[arg-type]
            feedback_reason=message.feedback_reason if with_feedback else None,  # type: ignore[arg-type]
            feedback_comment=message.feedback_comment if with_feedback else None,
            siblings=view.siblings,
            created_at=message.created_at,
        )


class ShareResponse(BaseModel):
    """Ссылка на /shared/{token} в приложении; открывают коллеги по компании."""

    token: str
    shared_at: datetime


class ConversationResponse(ConversationSummary):
    """Диалог: показанная ветка от первого вопроса до current_message_id."""

    current_message_id: UUID | None
    messages: list[MessageResponse]
    share: ShareResponse | None


class SharedConversationResponse(BaseModel):
    title: str
    owner_name: str
    shared_at: datetime
    messages: list[MessageResponse]


class AskRequest(RequestModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=5)


class MessageRequest(AskRequest):
    """parent_id — ответ, за которым идёт вопрос (обычно — последний
    показанный). Ответ из середины ветки или null у непустого диалога —
    правка вопроса: появится новая версия, прежняя останется."""

    parent_id: UUID | None


class ConversationUpdateRequest(RequestModel):
    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE_CHARS)
    pinned: bool | None = None


class SelectMessageRequest(RequestModel):
    message_id: UUID


class FeedbackRequest(RequestModel):
    """value=null — снять оценку. reason — только к -1."""

    value: Literal[-1, 1] | None
    reason: FeedbackReason | None = None
    comment: str | None = Field(default=None, max_length=MAX_FEEDBACK_COMMENT)


class SuggestionResponse(BaseModel):
    id: UUID
    text: str


class SuggestionsResponse(BaseModel):
    """company — заданные администратором; frequent — частые вопросы
    компании, обезличенно: заданные не меньше чем тремя разными людьми за
    последние 90 дней и получившие ответ по документам (без 👎)."""

    company: list[SuggestionResponse]
    frequent: list[str]


class SuggestionRequest(RequestModel):
    text: str = Field(min_length=1, max_length=MAX_SUGGESTION_TEXT, pattern=r"\S")


class SuggestionOrderRequest(RequestModel):
    ids: list[UUID] = Field(max_length=50)


# --- поток ответа (text/event-stream) ----------------------------------------
# Каждое событие — строка «data: <json>» и пустая строка; «: ping» —
# поддержание соединения. Первым всегда start, последним — done или error.


class StreamStart(BaseModel):
    type: Literal["start"] = "start"
    conversation: ConversationSummary
    question: MessageResponse
    answer: MessageResponse


class StreamStage(BaseModel):
    """searching — ищем в документах; writing — модель пишет ответ."""

    type: Literal["stage"] = "stage"
    stage: Literal["searching", "writing"]


class StreamOrigin(BaseModel):
    """Ответ будет не по документам — плашку можно показать сразу."""

    type: Literal["origin"] = "origin"
    origin: AnswerOrigin


class StreamDelta(BaseModel):
    type: Literal["delta"] = "delta"
    text: str


class StreamReset(BaseModel):
    """Показанный текст убрать: дальше пойдёт другой ответ."""

    type: Literal["reset"] = "reset"


class StreamDone(BaseModel):
    """Итог: answer.content заменяет напечатанный текст (ссылки уже
    нормализованы, у общего ответа — пометка). siblings у answer пустой —
    версии пришли в start."""

    type: Literal["done"] = "done"
    answer: MessageResponse
    diagnostics: AnswerDiagnosticsResponse | None = None


class StreamError(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str
    answer: MessageResponse | None


class ChatStreamEvent(
    RootModel[
        Annotated[
            StreamStart
            | StreamStage
            | StreamOrigin
            | StreamDelta
            | StreamReset
            | StreamDone
            | StreamError,
            Field(discriminator="type"),
        ]
    ]
):
    """Событие потока ответа."""
