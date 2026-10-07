"""Диалоги чата (ТЗ §6): список, карточка, вопрос с ответом по мере
генерации, «Остановить», «Ответить заново», правка вопроса, версии,
оценка с комментарием, «поделиться» внутри компании.

Свои диалоги видит только сам человек: здесь нет ни одной ручки, через
которую администратор открыл бы чужой диалог.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from corp_ed.api.v1.dependencies import (
    get_chat_generator,
    get_chat_runner,
    get_chat_service,
    get_current_user,
    get_stop_signals,
)
from corp_ed.api.v1.rate_limits import (
    CHAT_EDIT_PER_USER,
    FAQ_PER_USER,
    SHARED_VIEW_PER_USER,
    limit_by_user,
)
from corp_ed.api.v1.schemas.chat import (
    AskRequest,
    ChatStreamEvent,
    ConversationListResponse,
    ConversationResponse,
    ConversationSummary,
    ConversationUpdateRequest,
    FeedbackRequest,
    MessageRequest,
    MessageResponse,
    SelectMessageRequest,
    SharedConversationResponse,
    ShareResponse,
    StreamDelta,
    StreamDone,
    StreamError,
    StreamOrigin,
    StreamReset,
    StreamStage,
    StreamStart,
)
from corp_ed.api.v1.schemas.faq import AnswerDiagnosticsResponse
from corp_ed.domain.models import User
from corp_ed.services.chat_generation import (
    ChatEvent,
    ChatGenerator,
    ChatRunner,
    DeltaEvent,
    DoneEvent,
    ErrorEvent,
    GenerationJob,
    OriginEvent,
    ResetEvent,
    StageEvent,
    StopSignals,
)
from corp_ed.services.chat_service import (
    UNSET,
    ChatService,
    ConversationView,
    TurnStart,
)

router = APIRouter(prefix="/conversations", tags=["chat"])

Member = Annotated[User, Depends(get_current_user)]
Chat = Annotated[ChatService, Depends(get_chat_service)]
Generator = Annotated[ChatGenerator, Depends(get_chat_generator)]
Runner = Annotated[ChatRunner, Depends(get_chat_runner)]

KEEPALIVE_SECONDS = 15.0
"""Пустое событие, пока идёт поиск или повтор у провайдера: прокси не
рвёт молчащее соединение (proxy_read_timeout)."""

STREAM_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "model": ChatStreamEvent,
        "description": (
            "Поток text/event-stream: строки «data: <ChatStreamEvent>». Первым "
            "— start, последним — done или error. Разорванное соединение "
            "ответ не останавливает: он допишется и сохранится; остановить — "
            "POST …/stop."
        ),
        "content": {"text/event-stream": {}},
    }
}


@router.get("", response_model=ConversationListResponse)
async def list_conversations(
    chat: Chat,
    member: Member,
    q: Annotated[str | None, Query(max_length=200)] = None,
    before: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ConversationListResponse:
    """Свои диалоги. q — поиск по названию и тексту вопросов и ответов."""
    page = await chat.page(member, query=q, before=before, limit=limit)
    return ConversationListResponse(
        items=[ConversationSummary.of(item) for item in page.items],
        next_before=page.next_before,
    )


@router.post(
    "",
    response_class=StreamingResponse,
    response_model=None,
    responses=STREAM_RESPONSES,
    dependencies=[Depends(limit_by_user(FAQ_PER_USER))],
)
async def start_conversation(
    data: AskRequest,
    chat: Chat,
    generator: Generator,
    runner: Runner,
    member: Member,
) -> StreamingResponse:
    """Новый диалог с первым вопросом; название — по вопросу."""
    turn = await chat.begin(
        member,
        conversation_id=None,
        parent_id=None,
        question=data.question,
        attachment_ids=data.attachment_ids,
    )
    return _stream(turn, member, generator, runner)


@router.get(
    "/shared/{token}",
    response_model=SharedConversationResponse,
    dependencies=[Depends(limit_by_user(SHARED_VIEW_PER_USER))],
)
async def shared_conversation(
    token: Annotated[str, Path(max_length=64, pattern=r"^[A-Za-z0-9_-]+$")],
    chat: Chat,
    member: Member,
) -> SharedConversationResponse:
    """Диалог, которым поделился коллега по компании (только чтение)."""
    view = await chat.shared(member, token)
    return SharedConversationResponse(
        title=view.conversation.title,
        owner_name=view.owner_name,
        shared_at=view.conversation.shared_at or view.conversation.updated_at,
        messages=[MessageResponse.of(m, with_feedback=False) for m in view.messages],
    )


@router.get("/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(
    conversation_id: UUID, chat: Chat, member: Member
) -> ConversationResponse:
    return _conversation(await chat.get(member, conversation_id))


@router.patch(
    "/{conversation_id}",
    response_model=ConversationSummary,
    dependencies=[Depends(limit_by_user(CHAT_EDIT_PER_USER))],
)
async def update_conversation(
    conversation_id: UUID,
    data: ConversationUpdateRequest,
    chat: Chat,
    member: Member,
) -> ConversationSummary:
    """Переименовать, закрепить или открепить."""
    fields = data.model_dump(exclude_unset=True)
    conversation = await chat.update(
        member,
        conversation_id,
        title=fields["title"] if fields.get("title") else UNSET,
        pinned=fields["pinned"] if fields.get("pinned") is not None else UNSET,
    )
    return ConversationSummary.of(conversation)


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: UUID, chat: Chat, member: Member
) -> None:
    """Удалить диалог со всеми ветками и вложениями. Журнал вопросов
    (обезличенная статистика) остаётся."""
    await chat.delete(member, conversation_id)


@router.post(
    "/{conversation_id}/messages",
    response_class=StreamingResponse,
    response_model=None,
    responses=STREAM_RESPONSES,
    dependencies=[Depends(limit_by_user(FAQ_PER_USER))],
)
async def ask(
    conversation_id: UUID,
    data: MessageRequest,
    chat: Chat,
    generator: Generator,
    runner: Runner,
    member: Member,
) -> StreamingResponse:
    """Вопрос в диалоге или правка вопроса (см. MessageRequest)."""
    turn = await chat.begin(
        member,
        conversation_id=conversation_id,
        parent_id=data.parent_id,
        question=data.question,
        attachment_ids=data.attachment_ids,
    )
    return _stream(turn, member, generator, runner)


@router.post(
    "/{conversation_id}/messages/{message_id}/regenerate",
    response_class=StreamingResponse,
    response_model=None,
    responses=STREAM_RESPONSES,
    dependencies=[Depends(limit_by_user(FAQ_PER_USER))],
)
async def regenerate(
    conversation_id: UUID,
    message_id: UUID,
    chat: Chat,
    generator: Generator,
    runner: Runner,
    member: Member,
) -> StreamingResponse:
    """«Ответить заново» на вопрос message_id: прежний ответ — версия."""
    turn = await chat.begin_regenerate(member, conversation_id, message_id)
    return _stream(turn, member, generator, runner)


@router.post(
    "/{conversation_id}/messages/{message_id}/stop",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def stop(
    conversation_id: UUID,
    message_id: UUID,
    chat: Chat,
    signals: Annotated[StopSignals, Depends(get_stop_signals)],
    member: Member,
) -> None:
    """«Остановить»: ответ сохранится таким, каким успел быть. Уже готовый
    ответ — без изменений, тоже 204."""
    if await chat.check_stop(member, conversation_id, message_id):
        await signals.request(message_id)


@router.put(
    "/{conversation_id}/messages/{message_id}/feedback",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(limit_by_user(CHAT_EDIT_PER_USER))],
)
async def rate(
    conversation_id: UUID,
    message_id: UUID,
    data: FeedbackRequest,
    chat: Chat,
    member: Member,
) -> None:
    """👍/👎 и «что не так». Администратор видит оценки только обезличенно."""
    await chat.rate(
        member,
        conversation_id,
        message_id,
        value=data.value,
        reason=data.reason,
        comment=data.comment,
    )


@router.put("/{conversation_id}/current", response_model=ConversationResponse)
async def select_version(
    conversation_id: UUID,
    data: SelectMessageRequest,
    chat: Chat,
    member: Member,
) -> ConversationResponse:
    """Показать другую версию сообщения (стрелки ‹ 1/2 ›)."""
    return _conversation(await chat.select(member, conversation_id, data.message_id))


@router.post(
    "/{conversation_id}/share",
    response_model=ShareResponse,
    dependencies=[Depends(limit_by_user(CHAT_EDIT_PER_USER))],
)
async def share(conversation_id: UUID, chat: Chat, member: Member) -> ShareResponse:
    """Ссылка для коллег по компании на то, что видно сейчас. Повторно —
    обновить снимок, ссылка та же."""
    conversation = await chat.share(member, conversation_id)
    return ShareResponse(
        token=conversation.share_token or "",
        shared_at=conversation.shared_at or conversation.updated_at,
    )


@router.delete("/{conversation_id}/share", status_code=status.HTTP_204_NO_CONTENT)
async def unshare(conversation_id: UUID, chat: Chat, member: Member) -> None:
    """Закрыть ссылку: она перестанет открываться."""
    await chat.unshare(member, conversation_id)


def _conversation(view: ConversationView) -> ConversationResponse:
    conversation = view.conversation
    summary = ConversationSummary.of(conversation)
    return ConversationResponse(
        **summary.model_dump(),
        current_message_id=conversation.current_message_id,
        messages=[MessageResponse.of(m) for m in view.messages],
        share=(
            ShareResponse(
                token=conversation.share_token, shared_at=conversation.shared_at
            )
            if conversation.share_token and conversation.shared_at
            else None
        ),
    )


def _stream(
    turn: TurnStart, member: User, generator: ChatGenerator, runner: ChatRunner
) -> StreamingResponse:
    """Запустить ответ в фоне и пересылать его события клиенту."""
    queue: asyncio.Queue[ChatEvent] = asyncio.Queue()
    runner.start(generator.run(GenerationJob.from_turn(turn, member), queue.put_nowait))
    start = StreamStart(
        conversation=ConversationSummary.of(turn.conversation),
        question=MessageResponse.of(turn.question),
        answer=MessageResponse.of(turn.answer),
    )
    return StreamingResponse(
        _events(start, queue),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # nginx: не копить поток в буфере (deploy/nginx/kronto.conf).
            "X-Accel-Buffering": "no",
        },
    )


async def _events(
    start: StreamStart, queue: "asyncio.Queue[ChatEvent]"
) -> AsyncIterator[str]:
    yield _sse(start)
    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
        except TimeoutError:
            yield ": ping\n\n"
            continue
        yield _sse(_schema(event))
        if isinstance(event, DoneEvent | ErrorEvent):
            return


def _sse(event: BaseModel) -> str:
    return f"data: {event.model_dump_json()}\n\n"


def _schema(event: ChatEvent) -> BaseModel:
    if isinstance(event, StageEvent):
        return StreamStage(stage=event.stage)  # type: ignore[arg-type]
    if isinstance(event, OriginEvent):
        return StreamOrigin(origin=event.origin)
    if isinstance(event, DeltaEvent):
        return StreamDelta(text=event.text)
    if isinstance(event, ResetEvent):
        return StreamReset()
    if isinstance(event, DoneEvent):
        return StreamDone(
            answer=MessageResponse.of(event.answer),
            diagnostics=(
                AnswerDiagnosticsResponse.model_validate(event.diagnostics)
                if event.diagnostics
                else None
            ),
        )
    return StreamError(
        code=event.code,
        message=event.message,
        answer=MessageResponse.of(event.answer) if event.answer else None,
    )
