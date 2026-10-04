"""Подсказки вопросов на пустом экране чата (ТЗ §6)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from corp_ed.api.v1.dependencies import (
    get_current_user,
    get_suggestion_service,
    require_role,
)
from corp_ed.api.v1.rate_limits import SUGGESTION_EDIT_PER_TENANT, limit_by_tenant
from corp_ed.api.v1.schemas.chat import (
    SuggestionOrderRequest,
    SuggestionRequest,
    SuggestionResponse,
    SuggestionsResponse,
)
from corp_ed.domain.models import ChatSuggestion, User, UserRole
from corp_ed.services.suggestion_service import SuggestionService

router = APIRouter(prefix="/suggestions", tags=["chat"])

Service = Annotated[SuggestionService, Depends(get_suggestion_service)]
Admin = Annotated[User, Depends(require_role(UserRole.ADMIN))]
EDIT_LIMIT = [Depends(limit_by_tenant(SUGGESTION_EDIT_PER_TENANT))]


def _item(suggestion: ChatSuggestion) -> SuggestionResponse:
    return SuggestionResponse(id=suggestion.id, text=suggestion.text)


@router.get("", response_model=SuggestionsResponse)
async def list_suggestions(
    service: Service,
    member: Annotated[User, Depends(get_current_user)],
) -> SuggestionsResponse:
    """Подсказки для пустого экрана: от администратора и частые вопросы
    компании (обезличенно, не меньше трёх разных людей)."""
    suggestions = await service.for_chat()
    return SuggestionsResponse(
        company=[_item(item) for item in suggestions.company],
        frequent=suggestions.frequent,
    )


@router.post(
    "",
    response_model=SuggestionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=EDIT_LIMIT,
)
async def create_suggestion(
    data: SuggestionRequest, service: Service, admin: Admin
) -> SuggestionResponse:
    return _item(await service.create(admin, data.text))


@router.put("/order", response_model=list[SuggestionResponse], dependencies=EDIT_LIMIT)
async def reorder_suggestions(
    data: SuggestionOrderRequest, service: Service, admin: Admin
) -> list[SuggestionResponse]:
    return [_item(item) for item in await service.reorder(admin, data.ids)]


@router.patch(
    "/{suggestion_id}", response_model=SuggestionResponse, dependencies=EDIT_LIMIT
)
async def update_suggestion(
    suggestion_id: UUID, data: SuggestionRequest, service: Service, admin: Admin
) -> SuggestionResponse:
    return _item(await service.update(admin, suggestion_id, data.text))


@router.delete(
    "/{suggestion_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=EDIT_LIMIT
)
async def delete_suggestion(
    suggestion_id: UUID, service: Service, admin: Admin
) -> None:
    await service.delete(admin, suggestion_id)
