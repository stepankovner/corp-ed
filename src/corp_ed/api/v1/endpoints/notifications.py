"""Колокольчик, настройки писем, первые шаги (ТЗ §8)."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends

from corp_ed.api.v1.dependencies import (
    get_current_user,
    get_notification_service,
    get_onboarding_service,
    require_role,
)
from corp_ed.api.v1.schemas.notification import (
    MarkReadRequest,
    NotificationResponse,
    NotificationSettingsRequest,
    NotificationSettingsResponse,
    NotificationsResponse,
    OnboardingResponse,
)
from corp_ed.domain.models import User, UserRole
from corp_ed.services.notification_service import NotificationService
from corp_ed.services.onboarding_service import Onboarding, OnboardingService

router = APIRouter(tags=["notifications"])

Member = Annotated[User, Depends(get_current_user)]
Admin = Annotated[User, Depends(require_role(UserRole.ADMIN))]
Service = Annotated[NotificationService, Depends(get_notification_service)]


async def _inbox(service: NotificationService, member: User) -> NotificationsResponse:
    inbox = await service.inbox(member)
    return NotificationsResponse(
        items=[
            NotificationResponse(
                id=item.id,
                kind=item.kind,  # type: ignore[arg-type]
                title=item.title,
                body=item.body,
                link=item.link,
                created_at=item.created_at,
                read=item.read_at is not None,
            )
            for item in inbox.items
        ],
        unread=inbox.unread,
    )


@router.get("/notifications", response_model=NotificationsResponse)
async def list_notifications(service: Service, member: Member) -> NotificationsResponse:
    """Последние 30 уведомлений и сколько не прочитано."""
    return await _inbox(service, member)


@router.post("/notifications/read", response_model=NotificationsResponse)
async def mark_read(
    body: MarkReadRequest, service: Service, member: Member
) -> NotificationsResponse:
    await service.mark_read(member, body.ids)
    return await _inbox(service, member)


@router.get("/notifications/settings", response_model=NotificationSettingsResponse)
async def read_settings(service: Service, admin: Admin) -> NotificationSettingsResponse:
    return NotificationSettingsResponse(**await service.settings(admin))


@router.put("/notifications/settings", response_model=NotificationSettingsResponse)
async def update_settings(
    body: NotificationSettingsRequest, service: Service, admin: Admin
) -> NotificationSettingsResponse:
    changes = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    return NotificationSettingsResponse(**await service.update_settings(admin, changes))


def _onboarding(state: Onboarding) -> OnboardingResponse:
    return OnboardingResponse(
        documents=state.documents,
        people=state.people,
        question=state.question,
        tips_seen=state.tips_seen,
        checklist_hidden=state.checklist_hidden,
    )


@router.get("/onboarding", response_model=OnboardingResponse)
async def read_onboarding(
    service: Annotated[OnboardingService, Depends(get_onboarding_service)],
    member: Member,
) -> OnboardingResponse:
    """Первые шаги: чек-лист администратора и подсказки сотруднику."""
    return _onboarding(await service.state(member))


@router.post("/onboarding/{step}", response_model=OnboardingResponse)
async def finish_onboarding_step(
    step: Literal["tips", "checklist"],
    service: Annotated[OnboardingService, Depends(get_onboarding_service)],
    member: Member,
) -> OnboardingResponse:
    """tips — подсказки показаны; checklist — чек-лист скрыт."""
    return _onboarding(await service.done(member, step))
