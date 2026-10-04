"""Главная админки — обезличенная аналитика (ТЗ §7)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from corp_ed.api.v1.dependencies import get_analytics_service, require_role
from corp_ed.api.v1.schemas.company import (
    AnalyticsResponse,
    DayStatsResponse,
    FeedbackCommentResponse,
    FrequentQuestionResponse,
)
from corp_ed.domain.models import User, UserRole
from corp_ed.services.analytics_service import AnalyticsService

router = APIRouter(prefix="/analytics", tags=["company"])


@router.get("", response_model=AnalyticsResponse)
async def overview(
    service: Annotated[AnalyticsService, Depends(get_analytics_service)],
    admin: Annotated[User, Depends(require_role(UserRole.ADMIN))],
    days: Annotated[int, Query(ge=7, le=90)] = 30,
) -> AnalyticsResponse:
    """Вопросы по дням, доля без ответа, оценки, активные сотрудники,
    частые вопросы — за последние days дней по времени компании."""
    data = await service.overview(days)
    return AnalyticsResponse(
        since=data.since,
        until=data.until,
        questions=data.questions,
        answered=data.answered,
        general=data.general,
        refused=data.refused,
        likes=data.likes,
        dislikes=data.dislikes,
        reasons=data.reasons,
        active_people=data.active_people,
        members=data.members,
        credits=data.credits,
        days=[
            DayStatsResponse(day=d.day, questions=d.questions, answered=d.answered)
            for d in data.days
        ],
        frequent=[
            FrequentQuestionResponse(
                question=f.question, asked=f.asked, people=f.people, answered=f.answered
            )
            for f in data.frequent
        ],
        comments=[
            FeedbackCommentResponse(
                created_at=c.created_at,
                question=c.question,
                reason=c.reason,
                comment=c.comment,
            )
            for c in data.comments
        ],
        open_gaps=data.open_gaps,
    )
