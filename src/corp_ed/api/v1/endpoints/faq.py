from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from corp_ed.api.v1.dependencies import (
    get_current_user,
    get_faq_service,
    require_role,
)
from corp_ed.api.v1.rate_limits import FAQ_PER_USER, SEARCH_PER_USER, limit_by_user
from corp_ed.api.v1.schemas.faq import (
    AnswerDiagnosticsResponse,
    FaqAnswerResponse,
    FaqQuestionRequest,
    FaqSearchMatch,
    FaqSearchRequest,
    FaqSearchResponse,
    FaqSourceResponse,
    FeedbackRequest,
)
from corp_ed.domain.models import User, UserRole
from corp_ed.services.faq_service import FaqService

router = APIRouter(prefix="/faq", tags=["faq"])


@router.post(
    "/ask",
    response_model=FaqAnswerResponse,
    dependencies=[Depends(limit_by_user(FAQ_PER_USER))],
)
async def ask_faq(
    data: FaqQuestionRequest,
    service: Annotated[FaqService, Depends(get_faq_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> FaqAnswerResponse:
    answer = await service.answer(data.question, current_user)
    # Модель, токены и расстояние — только админу: сотруднику они не нужны,
    # а расстояние — внутреннее свойство порога, клиенты не должны на него
    # опираться (решение от 17.09).
    diagnostics = (
        AnswerDiagnosticsResponse.model_validate(answer.diagnostics)
        if current_user.role is UserRole.ADMIN and answer.diagnostics
        else None
    )
    return FaqAnswerResponse(
        answer_id=answer.log_id,
        content=answer.content,
        answer_given=answer.answer_given,
        origin=answer.origin,
        sources=[FaqSourceResponse.model_validate(s) for s in answer.sources],
        diagnostics=diagnostics,
    )


@router.post(
    "/search",
    response_model=FaqSearchResponse,
    dependencies=[Depends(limit_by_user(SEARCH_PER_USER))],
)
async def search_faq(
    data: FaqSearchRequest,
    service: Annotated[FaqService, Depends(get_faq_service)],
    current_user: Annotated[User, Depends(require_role(UserRole.ADMIN))],
) -> FaqSearchResponse:
    """Отладка поиска для eval (BH-5): top-K с расстояниями, без порога и LLM.

    Только ADMIN: показывает сырые выдержки и расстояния, которых нет в
    продуктовом ответе. Компания — из токена, как везде.
    """
    matches = await service.search(data.question, data.limit)
    return FaqSearchResponse(
        matches=[
            FaqSearchMatch(
                chunk_id=match.id,
                material_id=match.material_id,
                material_title=match.title,
                position=match.position,
                heading_path=match.heading_path,
                content=match.content,
                distance=match.distance,
            )
            for match in matches
        ]
    )


@router.patch("/answers/{answer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def rate_answer(
    answer_id: UUID,
    data: FeedbackRequest,
    service: Annotated[FaqService, Depends(get_faq_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    """👍 (1) или 👎 (-1) к своему ответу. Нужно отчёту о пробелах и eval."""
    await service.rate(current_user, answer_id, data.value)
