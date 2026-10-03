"""Учётка человека: имя, почта, компании, заявки, удаление (ТЗ §2–4).

Ручки работают без выбранной компании: учётка существует сама по себе.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status

from corp_ed.api.v1.dependencies import (
    get_account_service,
    get_company_request_service,
    get_current_account,
)
from corp_ed.api.v1.rate_limits import (
    COMPANY_REQUEST_PER_ACCOUNT,
    EMAIL_CHANGE_PER_ACCOUNT,
    VERIFY_PER_IP,
    client_ip,
    enforce,
    get_rate_limiter,
)
from corp_ed.api.v1.schemas.account import (
    CompanyRequestCreate,
    CompanyRequestResponse,
    EmailChangeRequest,
    LeaveCompanyRequest,
    NameUpdateRequest,
    PasswordConfirmRequest,
)
from corp_ed.api.v1.schemas.auth import TokenRequest
from corp_ed.api.v1.session_cookie import clear_refresh_cookie
from corp_ed.core.rate_limit import RateLimiter
from corp_ed.domain.models import Account
from corp_ed.services.account_service import AccountService
from corp_ed.services.company_request_service import CompanyRequestService

router = APIRouter(prefix="/account", tags=["account"])

CurrentAccount = Annotated[Account, Depends(get_current_account)]
Service = Annotated[AccountService, Depends(get_account_service)]


@router.patch("", status_code=status.HTTP_204_NO_CONTENT)
async def update_name(
    data: NameUpdateRequest, account: CurrentAccount, service: Service
) -> None:
    await service.update_name(
        account, first_name=data.first_name, last_name=data.last_name
    )


@router.post("/email", status_code=status.HTTP_202_ACCEPTED)
async def request_email_change(
    data: EmailChangeRequest,
    account: CurrentAccount,
    service: Service,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> None:
    """Письмо со ссылкой на новый адрес; почта сменится после перехода."""
    await enforce(limiter, EMAIL_CHANGE_PER_ACCOUNT, str(account.id))
    await service.request_email_change(account, str(data.new_email), data.password)


@router.post("/email/confirm", status_code=status.HTTP_204_NO_CONTENT)
async def confirm_email_change(
    request: Request,
    data: TokenRequest,
    service: Service,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> None:
    """Ссылка из письма на новый адрес. Без входа: письмо могут открыть
    на другом устройстве."""
    await enforce(limiter, VERIFY_PER_IP, client_ip(request))
    await service.confirm_email_change(data.token)


@router.post("/email/revert", status_code=status.HTTP_204_NO_CONTENT)
async def revert_email_change(
    request: Request,
    data: TokenRequest,
    service: Service,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> None:
    """«Это не я» из письма на прежний адрес. Без входа: у владельца,
    скорее всего, его уже нет."""
    await enforce(limiter, VERIFY_PER_IP, client_ip(request))
    await service.revert_email_change(data.token)


@router.post("/leave", status_code=status.HTTP_204_NO_CONTENT)
async def leave_company(
    data: LeaveCompanyRequest, account: CurrentAccount, service: Service
) -> None:
    """Выйти из компании. Учётка остаётся."""
    await service.leave_company(account, data.tenant_id)


@router.post("/delete", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    response: Response,
    data: PasswordConfirmRequest,
    account: CurrentAccount,
    service: Service,
) -> None:
    """Удалить учётку (152-ФЗ). Пароль — подтверждение, что это владелец."""
    await service.delete_account(account, data.password)
    clear_refresh_cookie(response)


@router.get("/company-requests", response_model=list[CompanyRequestResponse])
async def list_company_requests(
    account: CurrentAccount,
    service: Annotated[CompanyRequestService, Depends(get_company_request_service)],
) -> list[CompanyRequestResponse]:
    return [
        CompanyRequestResponse.model_validate(r)
        for r in await service.list_mine(account)
    ]


@router.post(
    "/company-requests",
    response_model=CompanyRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_company_request(
    data: CompanyRequestCreate,
    account: CurrentAccount,
    service: Annotated[CompanyRequestService, Depends(get_company_request_service)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> CompanyRequestResponse:
    """Заявка «Подключить компанию»: одобряет команда Kronto."""
    await enforce(limiter, COMPANY_REQUEST_PER_ACCOUNT, str(account.id))
    created = await service.create(
        account,
        company_name=data.company_name,
        seats=data.seats,
        comment=data.comment,
    )
    return CompanyRequestResponse.model_validate(created)


@router.post(
    "/company-requests/{request_id}/cancel", response_model=CompanyRequestResponse
)
async def cancel_company_request(
    request_id: UUID,
    account: CurrentAccount,
    service: Annotated[CompanyRequestService, Depends(get_company_request_service)],
) -> CompanyRequestResponse:
    return CompanyRequestResponse.model_validate(
        await service.cancel(account, request_id)
    )
