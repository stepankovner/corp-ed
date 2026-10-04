"""«Написать в поддержку» (ТЗ §8): от учётки, с компанией или без."""

from typing import Annotated

from fastapi import APIRouter, Depends, status

from corp_ed.api.v1.dependencies import (
    Principal,
    get_principal,
    get_support_service,
)
from corp_ed.api.v1.rate_limits import (
    SUPPORT_PER_ACCOUNT,
    enforce,
    get_rate_limiter,
)
from corp_ed.api.v1.schemas.notification import SupportCreateRequest, SupportResponse
from corp_ed.core.rate_limit import RateLimiter
from corp_ed.domain.models import SupportRequest
from corp_ed.services.support_service import SupportService

router = APIRouter(prefix="/support", tags=["support"])

Service = Annotated[SupportService, Depends(get_support_service)]


def support_response(request: SupportRequest) -> SupportResponse:
    return SupportResponse(
        id=request.id,
        topic=request.topic,  # type: ignore[arg-type]
        message=request.message,
        status=request.status,  # type: ignore[arg-type]
        created_at=request.created_at,
    )


@router.post("", response_model=SupportResponse, status_code=status.HTTP_201_CREATED)
async def create_support_request(
    body: SupportCreateRequest,
    service: Service,
    principal: Annotated[Principal, Depends(get_principal)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> SupportResponse:
    """Обращение уходит команде kronto; ответ придёт на почту учётки."""
    await enforce(limiter, SUPPORT_PER_ACCOUNT, str(principal.account.id))
    request = await service.create(
        principal.account,
        principal.tenant.id if principal.tenant else None,
        body.topic,
        body.message,
    )
    return support_response(request)


@router.get("/mine", response_model=list[SupportResponse])
async def my_support_requests(
    service: Service, principal: Annotated[Principal, Depends(get_principal)]
) -> list[SupportResponse]:
    return [support_response(r) for r in await service.mine(principal.account)]
