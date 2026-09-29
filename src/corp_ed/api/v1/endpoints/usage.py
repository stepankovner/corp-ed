from typing import Annotated

from fastapi import APIRouter, Depends

from corp_ed.api.v1.dependencies import get_credit_service, require_role
from corp_ed.api.v1.schemas.usage import UsageResponse
from corp_ed.domain.models import User, UserRole
from corp_ed.services.credit_service import CreditService

router = APIRouter(prefix="/usage", tags=["usage"])


@router.get("", response_model=UsageResponse)
async def get_usage(
    service: Annotated[CreditService, Depends(get_credit_service)],
    current_user: Annotated[User, Depends(require_role(UserRole.ADMIN))],
) -> UsageResponse:
    """Пул кредитов своей компании: сколько потрачено и сколько осталось.

    Только ADMIN: пул общий, решать, что делать при исчерпании (докупить
    места, подождать месяц), — администратору компании. Компания — из
    токена, параметра tenant_id нет.
    """
    return UsageResponse.model_validate(await service.usage())
