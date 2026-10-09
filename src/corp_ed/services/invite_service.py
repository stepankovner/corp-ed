"""Приглашения в компанию: ссылка и код (решения 28.09 и 03.10, ТЗ §2).

Админ создаёт приглашение и отправляет ссылку или короткий код куда
удобно — в рабочий чат, почтой, голосом. Человек со своей учёткой kronto
(зарегистрированной до или прямо по приглашению) вступает в компанию.

Что держит приглашение безопасным:
- токен ссылки — 256 бит случайности, код — 40 бит; в базе только sha256,
  ссылка и код показываются админу один раз;
- токен — после «#» в адресе (/join#<токен>): фрагмент не уходит на
  сервер, в журналы доступа и в Referer; в API он идёт телом запроса;
- срок жизни (1–30 дней), лимит использований, отзыв, необязательный
  домен почты (почта учётки подтверждена при регистрации), необязательное
  одобрение администратором;
- лимит частоты по IP на предпросмотр и вступление — от перебора кодов;
- по приглашению админа вступают только сотрудники; приглашение
  администратора выдаёт лишь команда Kronto (cli).
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
    MembershipBlockedError,
    NotFoundError,
)
from corp_ed.core.security import (
    hash_refresh_token,
    hash_secret,
    new_invite_code,
    new_refresh_token,
    normalize_invite_code,
)
from corp_ed.core.tenant_context import require_tenant, tenant_scope
from corp_ed.domain.models import (
    Account,
    Invite,
    InviteLookup,
    MemberStatus,
    Tenant,
    User,
    UserRole,
)
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.repositories.invite_repository import InviteRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services.notification_service import (
    Notice,
    NotificationKind,
    NotificationService,
)
from corp_ed.services.seats import JOIN_SEATS_MESSAGE, ensure_free_seat

logger = structlog.get_logger()

DEFAULT_TTL_DAYS = 7
MAX_TTL_DAYS = 30
MAX_USES = 1000

InviteStatus = Literal["active", "expired", "revoked", "used_up"]
JoinOutcome = Literal["joined", "pending", "already_member"]


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


def secret_hash(secret: str) -> str:
    """Хеш, по которому ищется приглашение: код (8 знаков в любом виде)
    или токен ссылки. Префикс разводит пространства хешей."""
    code = normalize_invite_code(secret)
    if code is not None:
        return hash_secret(f"invite-code:{code}")
    return hash_refresh_token(secret.strip())


@dataclass(frozen=True)
class CreatedInvite:
    invite: Invite
    token: str
    """Показывается один раз; в базе только его sha256."""
    code: str
    """Короткая форма для диктовки (K7QM-4XPA); показывается один раз."""


@dataclass(frozen=True)
class InvitePreview:
    company_name: str
    expires_at: datetime
    email_domain: str | None
    requires_approval: bool


@dataclass(frozen=True)
class JoinResult:
    member: User
    outcome: JoinOutcome


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
        actor: User | None,
        *,
        ttl_days: int = DEFAULT_TTL_DAYS,
        max_uses: int | None = None,
        email_domain: str | None = None,
        requires_approval: bool = False,
        role: UserRole = UserRole.EMPLOYEE,
    ) -> CreatedInvite:
        """Новое приглашение. max_uses по умолчанию — число мест компании:
        одна ссылка на всю команду. actor=None — выдала команда (cli)."""
        tenant = await self._tenant(require_tenant())
        uses = max_uses if max_uses is not None else min(tenant.seats, MAX_USES)
        token = new_refresh_token()
        code = new_invite_code()
        invite = await self.invites.add(
            Invite(
                token_hash=hash_refresh_token(token),
                code_hash=secret_hash(code),
                created_by=actor.id if actor else None,
                expires_at=_now() + timedelta(days=ttl_days),
                max_uses=uses,
                email_domain=email_domain,
                requires_approval=requires_approval,
                role=role,
            )
        )
        for hash_ in (invite.token_hash, invite.code_hash):
            self.invites.add_lookup(
                InviteLookup(hash=hash_, tenant_id=tenant.id, invite_id=invite.id)
            )
        self.audit.record(
            AuditAction.INVITE_CREATED,
            tenant_id=tenant.id,
            actor_id=actor.id if actor else None,
            target_type="invite",
            target_id=invite.id,
            details={
                "ttl_days": ttl_days,
                "max_uses": uses,
                "email_domain": email_domain,
                "requires_approval": requires_approval,
                "role": role.value,
            },
        )
        await self.session.commit()
        logger.info("invite_created", invite_id=str(invite.id), max_uses=uses)
        return CreatedInvite(invite=invite, token=token, code=code)

    async def list_recent(self) -> list[Invite]:
        return await self.invites.list_recent()

    async def revoke(self, actor: User, invite_id: UUID) -> Invite:
        invite = await self.invites.get(invite_id)
        if invite is None:
            raise NotFoundError("Приглашение не найдено")
        if invite.revoked_at is None:
            invite.revoked_at = _now()
            await self.invites.drop_lookups(invite.id)
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

    # --- человек с приглашением -----------------------------------------------

    async def preview(self, secret: str) -> InvitePreview:
        """В какую компанию ведёт приглашение — для карточки «Вступить»."""
        tenant, invite_id = await self._resolve(secret)
        with tenant_scope(tenant.id):
            invite = await self.invites.get(invite_id)
            if invite is None or invite_status(invite, _now()) != "active":
                raise InvalidInviteError()
            return InvitePreview(
                company_name=tenant.name,
                expires_at=invite.expires_at,
                email_domain=invite.email_domain,
                requires_approval=invite.requires_approval,
            )

    async def is_valid(self, secret: str) -> bool:
        try:
            await self.preview(secret)
        except InvalidInviteError:
            return False
        return True

    async def accept(self, account: Account, secret: str) -> JoinResult:
        """Вступить в компанию своей учёткой. Не коммитит: вызывающий
        выдаёт токены сессии с этой компанией в той же транзакции."""
        tenant, invite_id = await self._resolve(secret)
        with tenant_scope(tenant.id):
            invite = await self.invites.get_for_update(invite_id)
            if invite is None or invite_status(invite, _now()) != "active":
                raise InvalidInviteError()
            if invite.email_domain and not email_in_domain(
                account.email, invite.email_domain
            ):
                raise InviteEmailDomainError(invite.email_domain)
            # Домены почты компании (ТЗ §7) — для любого приглашения.
            domains = list(tenant.email_domains or [])
            if domains and not any(email_in_domain(account.email, d) for d in domains):
                raise InviteEmailDomainError(", @".join(domains), company=True)

            member = await self.users.get_by_account(account.id)
            if member is not None and member.status is MemberStatus.ACTIVE:
                return JoinResult(member, "already_member")
            if member is not None and member.status is MemberStatus.BLOCKED:
                raise MembershipBlockedError()
            if member is not None and member.status is MemberStatus.PENDING:
                return JoinResult(member, "pending")

            status = (
                MemberStatus.PENDING
                if invite.requires_approval
                else MemberStatus.ACTIVE
            )
            if status is MemberStatus.ACTIVE:
                await ensure_free_seat(
                    self.session, tenant.id, message=JOIN_SEATS_MESSAGE
                )
            if member is None:
                member = await self.users.create(
                    User(account_id=account.id, role=invite.role, status=status)
                )
            else:
                # Ушёл раньше — возвращается с ролью нового приглашения.
                member.status = status
                member.role = invite.role
                member.left_at = None
                member.data_purged_at = None
                member.token_version += 1
            invite.uses += 1
            self.audit.record(
                AuditAction.USER_JOINED_BY_INVITE
                if status is MemberStatus.ACTIVE
                else AuditAction.USER_JOIN_REQUESTED,
                tenant_id=tenant.id,
                actor_id=member.id,
                target_type="user",
                target_id=member.id,
                details={"invite_id": str(invite.id), "account_id": str(account.id)},
            )
            if status is MemberStatus.PENDING:
                # Администраторам (ТЗ §8): человек ждёт одобрения.
                who = account.full_name or account.email
                await NotificationService(self.session).notify_admins(
                    tenant.id,
                    Notice(
                        kind=NotificationKind.JOIN_REQUEST,
                        title=f"Заявка на вступление: {who}",
                        lines=[
                            f"{who} ({account.email}) хочет вступить в компанию "
                            "по приглашению.",
                            "Одобрить или отклонить — в разделе «Сотрудники».",
                        ],
                        link="/admin/users",
                        action="Открыть заявки",
                    ),
                )
            # Записать в контексте компании: коммит вызывающего идёт уже
            # вне его, и RLS не нашёл бы строки членства и приглашения.
            await self.session.flush()
        logger.info(
            "user_joined_by_invite",
            user_id=str(member.id),
            tenant_id=str(tenant.id),
            invite_id=str(invite.id),
            status=status.value,
        )
        return JoinResult(
            member, "joined" if status is MemberStatus.ACTIVE else "pending"
        )

    async def _resolve(self, secret: str) -> tuple[Tenant, UUID]:
        lookup = await self.invites.find_lookup(secret_hash(secret))
        if lookup is None:
            raise InvalidInviteError()
        tenant = await self.tenants.get_by_id(lookup.tenant_id)
        if tenant is None or not tenant.is_active:
            raise InvalidInviteError()
        return tenant, lookup.invite_id

    async def _tenant(self, tenant_id: UUID) -> Tenant:
        tenant = await self.tenants.get_by_id(tenant_id)
        if tenant is None:
            raise NotFoundError("Компания не найдена")
        return tenant


def _now() -> datetime:
    return datetime.now(UTC)
