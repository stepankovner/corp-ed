from typing import Annotated

from fastapi import APIRouter, Depends

from corp_ed.api.v1.dependencies import get_credit_service, require_role
from corp_ed.api.v1.schemas.usage import UsageResponse
from corp_ed.core.config import BillingSettings, get_billing_settings
from corp_ed.domain.credits import CreditUsage
from corp_ed.domain.models import User, UserRole
from corp_ed.services.credit_service import CreditService

router = APIRouter(prefix="/usage", tags=["usage"])


def usage_response(usage: CreditUsage, billing: BillingSettings) -> UsageResponse:
    return UsageResponse(
        period_start=usage.period_start,
        period_end=usage.period_end,
        seats=usage.seats,
        credits_per_seat=usage.credits_per_seat,
        pool=usage.pool,
        used=usage.used,
        remaining=usage.remaining,
        exhausted=usage.exhausted,
        warn_at_percent=usage.warn_at_percent,
        warning=usage.warning,
        purchased=usage.purchased,
        purchased_expires_at=usage.purchased_expires_at,
        purchased_expiring=usage.purchased_expiring,
        stopped=usage.stopped,
        avg_credits_per_question=billing.avg_credits_per_question,
    )


@router.get("", response_model=UsageResponse)
async def get_usage(
    service: Annotated[CreditService, Depends(get_credit_service)],
    billing: Annotated[BillingSettings, Depends(get_billing_settings)],
    current_user: Annotated[User, Depends(require_role(UserRole.ADMIN))],
) -> UsageResponse:
    """Кредиты своей компании: месячный пул (потрачено, осталось, до
    какого числа) и купленные кредиты (остаток, ближайшее сгорание).

    Только ADMIN: решать, что делать при исчерпании (купить пакет,
    добавить места, подождать месяц), — администратору компании.
    Компания — из токена, параметра tenant_id нет.
    """
    return usage_response(await service.usage(), billing)
