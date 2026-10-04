"""Песочница на сайте (ТЗ §1): kronto отвечает без входа по документам
вымышленной компании. Устройство и ограничения — services/demo_service.py."""

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Request

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
)
from corp_ed.core.rate_limit import RateLimiter
from corp_ed.domain.types import AnswerOrigin
from corp_ed.services.demo_service import DemoService
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


@router.post("/ask", response_model=DemoAnswerResponse)
async def demo_ask(
    request: Request,
    data: DemoQuestionRequest,
    service: Service,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> DemoAnswerResponse:
    """Вопрос без входа: ответ целиком, без истории. Лимиты — по IP и
    общий суточный; без Redis — 503 (вопрос стоит вызова модели)."""
    await enforce(limiter, DEMO_PER_IP, client_ip(request))
    await enforce(limiter, DEMO_PER_DAY, "all")
    if data.website:
        # Бот заполнил скрытое поле: отвечаем как на вопрос вне документов,
        # модель не вызываем.
        logger.info("demo_honeypot")
        return DemoAnswerResponse(
            content=REFUSAL_ANSWER, origin=AnswerOrigin.NONE, sources=[]
        )
    answer = await service.ask(data.question)
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
