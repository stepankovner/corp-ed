from typing import Annotated

from fastapi import APIRouter, Depends

from corp_ed.api.v1.dependencies import get_current_user, get_faq_service
from corp_ed.api.v1.schemas.faq import (
    FaqAnswerResponse,
    FaqQuestionRequest,
)
from corp_ed.domain.models import User
from corp_ed.services.faq_service import FaqService

router = APIRouter(prefix="/faq", tags=["faq"])


@router.post("/ask", response_model=FaqAnswerResponse)
async def ask_faq(
    data: FaqQuestionRequest,
    service: Annotated[FaqService, Depends(get_faq_service)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> FaqAnswerResponse:
    answer = await service.answer(data.question)
    return FaqAnswerResponse.model_validate(answer)
