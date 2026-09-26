from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from corp_ed.api.v1.dependencies import get_glossary_service, require_role
from corp_ed.api.v1.rate_limits import GLOSSARY_PER_TENANT, limit_by_tenant
from corp_ed.api.v1.schemas.glossary import (
    GlossaryTermCreateRequest,
    GlossaryTermResponse,
    GlossaryTermUpdateRequest,
)
from corp_ed.domain.models import User, UserRole
from corp_ed.services.glossary_service import GlossaryService

router = APIRouter(prefix="/glossary", tags=["glossary"])

AdminUser = Annotated[User, Depends(require_role(UserRole.ADMIN))]
Service = Annotated[GlossaryService, Depends(get_glossary_service)]


@router.get("", response_model=list[GlossaryTermResponse])
async def list_terms(
    service: Service, current_user: AdminUser
) -> list[GlossaryTermResponse]:
    """Словарь сокращений своей компании (M5, BH-14)."""
    terms = await service.list_all()
    return [GlossaryTermResponse.model_validate(term) for term in terms]


@router.post(
    "",
    response_model=GlossaryTermResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(limit_by_tenant(GLOSSARY_PER_TENANT))],
)
async def create_term(
    data: GlossaryTermCreateRequest, service: Service, current_user: AdminUser
) -> GlossaryTermResponse:
    """Добавить сокращение. Действует со следующего вопроса: расшифровка
    дописывается к вопросу перед поиском, переиндексация не нужна."""
    term = await service.create(current_user, term=data.term, expansion=data.expansion)
    return GlossaryTermResponse.model_validate(term)


@router.patch(
    "/{term_id}",
    response_model=GlossaryTermResponse,
    dependencies=[Depends(limit_by_tenant(GLOSSARY_PER_TENANT))],
)
async def update_term(
    term_id: UUID,
    data: GlossaryTermUpdateRequest,
    service: Service,
    current_user: AdminUser,
) -> GlossaryTermResponse:
    term = await service.update(
        current_user, term_id, term=data.term, expansion=data.expansion
    )
    return GlossaryTermResponse.model_validate(term)


@router.delete(
    "/{term_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(limit_by_tenant(GLOSSARY_PER_TENANT))],
)
async def delete_term(term_id: UUID, service: Service, current_user: AdminUser) -> None:
    await service.delete(current_user, term_id)
