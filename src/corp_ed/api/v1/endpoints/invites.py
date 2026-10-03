from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status

from corp_ed.api.v1.dependencies import (
    get_auth_service,
    get_current_account,
    get_invite_service,
    get_tenant_repository,
    require_role,
)
from corp_ed.api.v1.rate_limits import (
    INVITE_PER_IP,
    client_ip,
    enforce,
    get_rate_limiter,
)
from corp_ed.api.v1.schemas.invite import (
    InviteCreatedResponse,
    InviteCreateRequest,
    InvitePreviewResponse,
    InviteResponse,
    InviteSecretRequest,
    JoinResponse,
)
from corp_ed.api.v1.session_cookie import read_refresh_cookie, session_response
from corp_ed.core.rate_limit import RateLimiter
from corp_ed.domain.models import Account, Invite, User, UserRole
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.services.auth_service import AuthService
from corp_ed.services.invite_service import InviteService, invite_status

router = APIRouter(prefix="/invites", tags=["invites"])

AdminUser = Annotated[User, Depends(require_role(UserRole.ADMIN))]
Service = Annotated[InviteService, Depends(get_invite_service)]


def _response(invite: Invite) -> InviteResponse:
    return InviteResponse(
        id=invite.id,
        created_at=invite.created_at,
        expires_at=invite.expires_at,
        max_uses=invite.max_uses,
        uses=invite.uses,
        email_domain=invite.email_domain,
        requires_approval=invite.requires_approval,
        status=invite_status(invite, datetime.now(UTC)),
    )


# --- администратор компании ---------------------------------------------------


@router.post(
    "", response_model=InviteCreatedResponse, status_code=status.HTTP_201_CREATED
)
async def create_invite(
    data: InviteCreateRequest, service: Service, current_user: AdminUser
) -> InviteCreatedResponse:
    """Приглашение в свою компанию. Ссылка и код — в ответе один раз."""
    created = await service.create(
        current_user,
        ttl_days=data.ttl_days,
        max_uses=data.max_uses,
        email_domain=data.email_domain,
        requires_approval=data.requires_approval,
    )
    return InviteCreatedResponse(
        invite=_response(created.invite), token=created.token, code=created.code
    )


@router.get("", response_model=list[InviteResponse])
async def list_invites(
    service: Service, current_user: AdminUser
) -> list[InviteResponse]:
    """Последние приглашения компании с их состоянием; ссылок и кодов нет."""
    return [_response(invite) for invite in await service.list_recent()]


@router.delete("/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invite(
    invite_id: UUID, service: Service, current_user: AdminUser
) -> None:
    await service.revoke(current_user, invite_id)


# --- человек с приглашением ---------------------------------------------------
# POST, а не GET с токеном в адресе: токен не должен попасть в журналы.


@router.post("/preview", response_model=InvitePreviewResponse)
async def preview_invite(
    request: Request,
    data: InviteSecretRequest,
    service: Service,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> InvitePreviewResponse:
    """В какую компанию ведёт приглашение — до входа, для карточки
    «Вступить»."""
    await enforce(limiter, INVITE_PER_IP, client_ip(request))
    preview = await service.preview(data.secret)
    return InvitePreviewResponse(
        company_name=preview.company_name,
        expires_at=preview.expires_at,
        email_domain=preview.email_domain,
        requires_approval=preview.requires_approval,
    )


@router.post("/accept", response_model=JoinResponse)
async def accept_invite(
    request: Request,
    response: Response,
    data: InviteSecretRequest,
    service: Service,
    account: Annotated[Account, Depends(get_current_account)],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    tenant_repo: Annotated[TenantRepository, Depends(get_tenant_repository)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> JoinResponse:
    """Вступить в компанию своей учёткой. Вступил — сессия сразу
    переключается на эту компанию; ждёт одобрения — сессия прежняя."""
    await enforce(limiter, INVITE_PER_IP, client_ip(request))
    result = await service.accept(account, data.secret)
    tenant = await tenant_repo.get_by_id(result.member.tenant_id)
    company_name = tenant.name if tenant else ""
    if result.outcome == "pending":
        await service.session.commit()
        return JoinResponse(outcome="pending", company_name=company_name, session=None)
    pair = await auth_service.switch_company(
        account, result.member.tenant_id, read_refresh_cookie(request)
    )
    return JoinResponse(
        outcome=result.outcome,
        company_name=company_name,
        session=session_response(response, pair),
    )
