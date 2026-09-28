from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status

from corp_ed.api.v1.dependencies import (
    get_auth_service,
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
from corp_ed.api.v1.schemas.auth import TokenResponse
from corp_ed.api.v1.schemas.invite import (
    InviteAcceptRequest,
    InviteCreatedResponse,
    InviteCreateRequest,
    InvitePreviewResponse,
    InviteResponse,
    InviteTokenRequest,
)
from corp_ed.api.v1.session_cookie import session_response
from corp_ed.core.rate_limit import RateLimiter
from corp_ed.domain.models import Invite, User, UserRole
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
        status=invite_status(invite, datetime.now(UTC)),
    )


# --- администратор компании ---------------------------------------------------


@router.post(
    "", response_model=InviteCreatedResponse, status_code=status.HTTP_201_CREATED
)
async def create_invite(
    data: InviteCreateRequest,
    service: Service,
    current_user: AdminUser,
    tenant_repo: Annotated[TenantRepository, Depends(get_tenant_repository)],
) -> InviteCreatedResponse:
    """Ссылка-приглашение в свою компанию. Токен — в ответе один раз."""
    created = await service.create(
        current_user,
        ttl_days=data.ttl_days,
        max_uses=data.max_uses,
        email_domain=data.email_domain,
    )
    tenant = await tenant_repo.get_by_id(current_user.tenant_id)
    return InviteCreatedResponse(
        invite=_response(created.invite),
        token=created.token,
        company_code=tenant.company_code if tenant else "",
    )


@router.get("", response_model=list[InviteResponse])
async def list_invites(
    service: Service, current_user: AdminUser
) -> list[InviteResponse]:
    """Последние ссылки компании с их состоянием; токенов в ответе нет."""
    return [_response(invite) for invite in await service.list_recent()]


@router.delete("/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invite(
    invite_id: UUID, service: Service, current_user: AdminUser
) -> None:
    await service.revoke(current_user, invite_id)


# --- человек со ссылкой, без входа --------------------------------------------
# POST, а не GET с токеном в адресе: токен не должен попасть в журналы.


@router.post("/preview", response_model=InvitePreviewResponse)
async def preview_invite(
    request: Request,
    data: InviteTokenRequest,
    service: Service,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> InvitePreviewResponse:
    """В какую компанию ведёт ссылка — для карточки «Присоединиться»."""
    await enforce(limiter, INVITE_PER_IP, client_ip(request))
    preview = await service.preview(data.company_code, data.token)
    return InvitePreviewResponse(
        company_name=preview.company_name,
        expires_at=preview.expires_at,
        email_domain=preview.email_domain,
    )


@router.post(
    "/accept", response_model=TokenResponse, status_code=status.HTTP_201_CREATED
)
async def accept_invite(
    request: Request,
    response: Response,
    data: InviteAcceptRequest,
    service: Service,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> TokenResponse:
    """Завести учётку сотрудника по ссылке и сразу войти."""
    await enforce(limiter, INVITE_PER_IP, client_ip(request))
    user = await service.accept(
        data.company_code,
        data.token,
        email=str(data.email),
        full_name=data.full_name,
        password=data.password,
    )
    pair = await auth_service.open_session(user)
    return session_response(response, pair)
