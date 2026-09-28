"""Ссылки-приглашения в компанию (решение владельца продукта 28.09).

Админ создаёт ссылку и отправляет её куда удобно — в мессенджер, почтой;
по ней человек сам заводит учётку сотрудника и сразу входит. Почтового
сервиса для этого не нужно.

Что держит ссылку безопасной:
- токен — 256 бит случайности, в базе только sha256 (как refresh-токены);
  ссылка показывается админу один раз;
- токен — после «#» в адресе (/join/<код>#<токен>): фрагмент не уходит
  на сервер, в журналы доступа и в Referer; в API он идёт телом запроса;
- срок жизни (1–30 дней), лимит использований, отзыв, необязательный
  домен почты;
- по ссылке заводится только сотрудник (EMPLOYEE), не админ;
- лимит частоты по IP на предпросмотр и присоединение.

Почта присоединившегося не подтверждается — почтового канала нет.
Админ видит новых сотрудников в списке и в журнале и может заблокировать
лишнего.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import (
    InvalidInviteError,
    InviteEmailDomainError,
    InviteEmailTakenError,
    NotFoundError,
)
from corp_ed.core.password_policy import validate_password
from corp_ed.core.security import hash_password, hash_refresh_token, new_refresh_token
from corp_ed.core.tenant_context import require_tenant, tenant_scope
from corp_ed.domain.models import Invite, Tenant, User, UserRole
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.repositories.invite_repository import InviteRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services.seats import JOIN_SEATS_MESSAGE, ensure_free_seat

logger = structlog.get_logger()

DEFAULT_TTL_DAYS = 7
MAX_TTL_DAYS = 30
MAX_USES = 1000

InviteStatus = Literal["active", "expired", "revoked", "used_up"]


def invite_status(invite: Invite, now: datetime) -> InviteStatus:
    if invite.revoked_at is not None:
        return "revoked"
    if invite.expires_at <= now:
        return "expired"
    if invite.uses >= invite.max_uses:
        return "used_up"
    return "active"


def email_in_domain(email: str, domain: str) -> bool:
    """Почта в домене или его поддомене: anna@acme.ru, anna@msk.acme.ru."""
    host = email.rsplit("@", 1)[-1].casefold()
    return host == domain or host.endswith(f".{domain}")


@dataclass(frozen=True)
class CreatedInvite:
    invite: Invite
    token: str
    """Показывается один раз; в базе только его sha256."""


@dataclass(frozen=True)
class InvitePreview:
    company_name: str
    expires_at: datetime
    email_domain: str | None


class InviteService:
    def __init__(
        self,
        invites: InviteRepository,
        tenants: TenantRepository,
        users: UserRepository,
        audit: AuditRepository,
        session: AsyncSession,
    ) -> None:
        self.invites = invites
        self.tenants = tenants
        self.users = users
        self.audit = audit
        self.session = session

    # --- администратор компании (тенант из токена) ----------------------------

    async def create(
        self,
        actor: User,
        *,
        ttl_days: int = DEFAULT_TTL_DAYS,
        max_uses: int | None = None,
        email_domain: str | None = None,
    ) -> CreatedInvite:
        """Новая ссылка. max_uses по умолчанию — число мест компании: одна
        ссылка на всю команду."""
        tenant = await self._tenant(require_tenant())
        uses = max_uses if max_uses is not None else min(tenant.seats, MAX_USES)
        token = new_refresh_token()
        invite = await self.invites.add(
            Invite(
                token_hash=hash_refresh_token(token),
                created_by=actor.id,
                expires_at=_now() + timedelta(days=ttl_days),
                max_uses=uses,
                email_domain=email_domain,
            )
        )
        self.audit.record(
            AuditAction.INVITE_CREATED,
            tenant_id=tenant.id,
            actor_id=actor.id,
            target_type="invite",
            target_id=invite.id,
            details={
                "ttl_days": ttl_days,
                "max_uses": uses,
                "email_domain": email_domain,
            },
        )
        await self.session.commit()
        logger.info("invite_created", invite_id=str(invite.id), max_uses=uses)
        return CreatedInvite(invite=invite, token=token)

    async def list_recent(self) -> list[Invite]:
        return await self.invites.list_recent()

    async def revoke(self, actor: User, invite_id: UUID) -> Invite:
        invite = await self.invites.get(invite_id)
        if invite is None:
            raise NotFoundError("Ссылка не найдена")
        if invite.revoked_at is None:
            invite.revoked_at = _now()
            self.audit.record(
                AuditAction.INVITE_REVOKED,
                tenant_id=invite.tenant_id,
                actor_id=actor.id,
                target_type="invite",
                target_id=invite.id,
            )
            await self.session.commit()
            logger.info("invite_revoked", invite_id=str(invite.id))
        return invite

    # --- человек со ссылкой (без входа) ---------------------------------------

    async def preview(self, company_code: str, token: str) -> InvitePreview:
        """В какую компанию ведёт ссылка — для карточки «Присоединиться»."""
        tenant = await self._active_tenant(company_code)
        with tenant_scope(tenant.id):
            invite = await self.invites.get_by_hash(hash_refresh_token(token))
            if invite is None or invite_status(invite, _now()) != "active":
                raise InvalidInviteError()
            return InvitePreview(
                company_name=tenant.name,
                expires_at=invite.expires_at,
                email_domain=invite.email_domain,
            )

    async def accept(
        self,
        company_code: str,
        token: str,
        *,
        email: str,
        full_name: str | None,
        password: str,
    ) -> User:
        """Завести сотрудника по ссылке. Не коммитит: вызывающий выдаёт
        токены входа в той же транзакции (AuthService.open_session)."""
        tenant = await self._active_tenant(company_code)
        email = email.strip().casefold()
        with tenant_scope(tenant.id):
            invite = await self.invites.get_by_hash_for_update(
                hash_refresh_token(token)
            )
            if invite is None or invite_status(invite, _now()) != "active":
                raise InvalidInviteError()
            if invite.email_domain and not email_in_domain(email, invite.email_domain):
                raise InviteEmailDomainError(invite.email_domain)
            validate_password(password, email=email)
            if await self.users.get_by_email(email) is not None:
                raise InviteEmailTakenError()
            await ensure_free_seat(self.session, tenant.id, message=JOIN_SEATS_MESSAGE)

            user = await self.users.create(
                User(
                    email=email,
                    full_name=full_name,
                    role=UserRole.EMPLOYEE,
                    hashed_password=hash_password(password),
                    # Пароль придумал сам человек — менять его незачем.
                    must_change_password=False,
                )
            )
            invite.uses += 1
            self.audit.record(
                AuditAction.USER_JOINED_BY_INVITE,
                tenant_id=tenant.id,
                actor_id=user.id,
                target_type="user",
                target_id=user.id,
                details={"invite_id": str(invite.id)},
            )
        logger.info(
            "user_joined_by_invite",
            user_id=str(user.id),
            tenant_id=str(tenant.id),
            invite_id=str(invite.id),
        )
        return user

    async def _active_tenant(self, company_code: str) -> Tenant:
        tenant = await self.tenants.get_by_company_code(company_code.strip().casefold())
        if tenant is None or not tenant.is_active:
            raise InvalidInviteError()
        return tenant

    async def _tenant(self, tenant_id: UUID) -> Tenant:
        tenant = await self.tenants.get_by_id(tenant_id)
        if tenant is None:
            raise NotFoundError("Компания не найдена")
        return tenant


def _now() -> datetime:
    return datetime.now(UTC)
