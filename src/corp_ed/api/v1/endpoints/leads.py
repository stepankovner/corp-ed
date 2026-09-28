from datetime import datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Request, status

from corp_ed.api.v1.dependencies import get_lead_service, get_team_notifier
from corp_ed.api.v1.rate_limits import (
    LEAD_PER_IP,
    LEADS_PER_DAY,
    client_ip,
    enforce,
    get_rate_limiter,
)
from corp_ed.api.v1.schemas.lead import (
    LeadFormResponse,
    LeadReceivedResponse,
    LeadRequest,
)
from corp_ed.core.rate_limit import RateLimiter
from corp_ed.domain.leads import CALL_SLOTS, CALL_TIMEZONE, call_dates
from corp_ed.services.lead_service import LeadDraft, LeadService
from corp_ed.services.team_notify import TeamNotifier, lead_message

logger = structlog.get_logger()

router = APIRouter(prefix="/leads", tags=["leads"])

Service = Annotated[LeadService, Depends(get_lead_service)]


def _today() -> datetime:
    return datetime.now(CALL_TIMEZONE)


@router.get("/form", response_model=LeadFormResponse)
async def lead_form(service: Service) -> LeadFormResponse:
    """Настройки формы записи на созвон — без входа."""
    settings = service.settings
    first, last = call_dates(_today().date(), settings.days_ahead)
    return LeadFormResponse(
        enabled=settings.enabled,
        policy_url=settings.policy_url or None,
        policy_version=settings.policy_version or None,
        slots=list(CALL_SLOTS),
        first_date=first,
        last_date=last,
        timezone=str(CALL_TIMEZONE),
    )


@router.post(
    "", response_model=LeadReceivedResponse, status_code=status.HTTP_201_CREATED
)
async def submit_lead(
    request: Request,
    data: LeadRequest,
    service: Service,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    notifier: Annotated[TeamNotifier, Depends(get_team_notifier)],
) -> LeadReceivedResponse:
    """Заявка на созвон со страницы тарифов (досье 10.1). Без входа:
    лимит по IP и общий суточный, ловушка для ботов, согласие с версией
    политики. Команда видит заявки в `cli leads list`."""
    service.ensure_open()
    await enforce(limiter, LEAD_PER_IP, client_ip(request))
    await enforce(limiter, LEADS_PER_DAY, "all")
    if data.website:
        # Бот заполнил скрытое поле: отвечаем как обычно, чтобы он не
        # подбирал обход, но не сохраняем.
        logger.info("lead_honeypot")
        return LeadReceivedResponse()
    lead = await service.submit(
        LeadDraft(
            company_name=data.company_name,
            contact_name=data.contact_name,
            phone=data.phone,
            email=str(data.email).casefold() if data.email else None,
            seats=data.seats,
            tariff=data.tariff,
            preferred_date=data.preferred_date,
            preferred_slot=data.preferred_slot,
            comment=data.comment or None,
            policy_version=data.policy_version,
            consent=data.consent,
        ),
        today=_today().date(),
    )
    notifier.notify(
        lead_message(
            tariff=lead.tariff,
            seats=lead.seats,
            preferred_date=lead.preferred_date,
            preferred_slot=lead.preferred_slot,
        )
    )
    return LeadReceivedResponse()
