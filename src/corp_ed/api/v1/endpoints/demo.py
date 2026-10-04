"""Песочница на сайте (ТЗ §1): kronto отвечает без входа по документам
вымышленной компании. Устройство и ограничения — services/demo_service.py."""

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from corp_ed.api.v1.dependencies import get_demo_service
from corp_ed.api.v1.rate_limits import (
    DEMO_PER_DAY,
    DEMO_PER_IP,
    client_ip,
    enforce,
    get_rate_limiter,
)
from corp_ed.api.v1.schemas.demo import (
    DemoAnswerResponse,
    DemoInfoResponse,
    DemoQuestionRequest,
    DemoSourceResponse,
    DemoStreamDelta,
    DemoStreamDone,
    DemoStreamError,
    DemoStreamEvent,
    DemoStreamReset,
    DemoStreamStage,
)
from corp_ed.core.exceptions import DemoUnavailableError
from corp_ed.core.rate_limit import RateLimiter
from corp_ed.domain.types import AnswerOrigin
from corp_ed.services.demo_service import DemoAnswer, DemoService
from corp_ed.services.general_answer import REFUSAL_ANSWER

logger = structlog.get_logger()

router = APIRouter(prefix="/demo", tags=["demo"])

Service = Annotated[DemoService, Depends(get_demo_service)]


@router.get("", response_model=DemoInfoResponse)
async def demo_info(service: Service) -> DemoInfoResponse:
    """Компания песочницы, её документы и готовые вопросы. 503 demo_off —
    песочница выключена или ещё не заведена."""
    info = await service.info()
    return DemoInfoResponse(
        company=info.company, documents=info.documents, questions=info.questions
    )


Limiter = Annotated[RateLimiter, Depends(get_rate_limiter)]

KEEPALIVE_SECONDS = 15.0
"""Пустое событие, пока идёт поиск: прокси не рвёт молчащее соединение."""

HONEYPOT_ANSWER = DemoAnswerResponse(
    content=REFUSAL_ANSWER, origin=AnswerOrigin.NONE, sources=[]
)

STREAM_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "model": DemoStreamEvent,
        "description": (
            "Поток text/event-stream: строки «data: <DemoStreamEvent>», "
            "последним — done или error. Посетитель закрыл страницу — ответ "
            "прерывается: он нигде не хранится."
        ),
        "content": {"text/event-stream": {}},
    }
}


@router.post("/ask", response_model=DemoAnswerResponse)
async def demo_ask(
    request: Request, data: DemoQuestionRequest, service: Service, limiter: Limiter
) -> DemoAnswerResponse:
    """Вопрос без входа: ответ целиком, без истории. Лимиты — по IP и
    общий суточный; без Redis — 503 (вопрос стоит вызова модели)."""
    if not await _admit(request, data, limiter):
        return HONEYPOT_ANSWER
    return _response(await service.ask(data.question))


@router.post(
    "/ask/stream",
    response_class=StreamingResponse,
    response_model=None,
    responses=STREAM_RESPONSES,
)
async def demo_ask_stream(
    request: Request, data: DemoQuestionRequest, service: Service, limiter: Limiter
) -> StreamingResponse:
    """То же, что /ask, но ответ печатается по мере генерации, как в чате.
    Лимиты и песочница без компании (503 demo_off) — до потока; пул или
    модель не отвечают — событие error в потоке."""
    if not await _admit(request, data, limiter):
        return _stream(_finished(DemoStreamDone(answer=HONEYPOT_ANSWER)))
    await service.info()
    queue: asyncio.Queue[BaseModel] = asyncio.Queue()

    async def run() -> None:
        try:
            answer = await service.ask(data.question, sink=_QueueSink(queue))
        except DemoUnavailableError as exc:
            queue.put_nowait(DemoStreamError(code=exc.code, message=str(exc)))
        except Exception:
            logger.exception("demo_stream_failed")
            queue.put_nowait(
                DemoStreamError(
                    code="internal",
                    message="Не получилось ответить. Попробуйте ещё раз.",
                )
            )
        else:
            queue.put_nowait(DemoStreamDone(answer=_response(answer)))

    return _stream(_events(run, queue))


async def _admit(
    request: Request, data: DemoQuestionRequest, limiter: RateLimiter
) -> bool:
    """Лимиты — по IP и общий суточный (без Redis — 503: вопрос стоит
    вызова модели). False — бот заполнил скрытое поле: отвечаем как на
    вопрос вне документов, модель не вызываем."""
    await enforce(limiter, DEMO_PER_IP, client_ip(request))
    await enforce(limiter, DEMO_PER_DAY, "all")
    if data.website:
        logger.info("demo_honeypot")
        return False
    return True


def _response(answer: DemoAnswer) -> DemoAnswerResponse:
    return DemoAnswerResponse(
        content=answer.content,
        origin=answer.origin,
        sources=[
            DemoSourceResponse(
                title=source.title,
                heading_path=source.heading_path,
                content=source.content,
            )
            for source in answer.sources
        ],
    )


class _QueueSink:
    """AnswerSink FaqService: ход ответа — в очередь потока."""

    def __init__(self, queue: "asyncio.Queue[BaseModel]") -> None:
        self._queue = queue

    async def stage(self, stage: str) -> None:
        self._queue.put_nowait(DemoStreamStage(stage=stage))  # type: ignore[arg-type]

    async def origin(self, origin: AnswerOrigin) -> None:
        """Плашки «не по документам» в песочнице нет: строгий режим."""

    async def delta(self, text: str) -> None:
        self._queue.put_nowait(DemoStreamDelta(text=text))

    async def reset(self) -> None:
        self._queue.put_nowait(DemoStreamReset())

    async def should_stop(self) -> bool:
        return False


def _stream(events: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # nginx: не копить поток в буфере (deploy/nginx/kronto.conf).
            "X-Accel-Buffering": "no",
        },
    )


async def _finished(event: BaseModel) -> AsyncIterator[str]:
    yield _sse(event)


async def _events(
    run: Callable[[], Coroutine[Any, Any, None]], queue: "asyncio.Queue[BaseModel]"
) -> AsyncIterator[str]:
    task = asyncio.create_task(run())
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            yield _sse(event)
            if isinstance(event, DemoStreamDone | DemoStreamError):
                return
    finally:
        # Посетитель закрыл страницу — ответ нигде не хранится, модель дальше
        # не зовём.
        task.cancel()


def _sse(event: BaseModel) -> str:
    return f"data: {event.model_dump_json()}\n\n"
