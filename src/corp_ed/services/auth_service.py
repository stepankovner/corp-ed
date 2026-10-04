"""Вход, сессии и переключение компаний (ТЗ §2–3, решение 03.10).

Вход — один на человека: учётка (почта и пароль), а не «компания +
почта». Сессия может быть без компании (человек ещё не вступил) или с
выбранной компанией; переключение выдаёт новую пару токенов. Права в
компании читаются из членства на каждый запрос (api/v1/dependencies).
"""

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import structlog
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.config import get_settings
from corp_ed.core.exceptions import (
    DomainError,
    EmailNotVerifiedError,
    InvalidCredentialsError,
    NotAuthenticatedError,
    NotFoundError,
    WeakPasswordError,
)
from corp_ed.core.password_policy import validate_password
from corp_ed.core.request_context import current_client_ip, current_user_agent
from corp_ed.core.security import (
    create_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    password_needs_rehash,
    verify_password,
)
from corp_ed.core.tenant_context import account_scope, tenant_scope
from corp_ed.domain.models import (
    Account,
    MemberStatus,
    RefreshToken,
    TrustedDevice,
    User,
)
from corp_ed.repositories.account_repository import AccountRepository
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.repositories.refresh_token_repository import RefreshTokenRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services import email_templates
from corp_ed.services.email_service import EmailService
from corp_ed.services.passwords import set_password

logger = structlog.get_logger()


class InvalidCurrentPasswordError(DomainError):
    """При смене пароля текущий указан неверно.

    Не InvalidCredentialsError: тот даёт 401, и клиент решил бы, что
    сессия кончилась, и выкинул пользователя на логин.
    """

    def __init__(self) -> None:
        super().__init__("Текущий пароль указан неверно")


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int
    """Срок жизни access-токена в секундах."""
    remember: bool = True
    """False — cookie без срока: сессия до закрытия браузера."""


class AuthService:
    def __init__(
        self,
        tenant_repo: TenantRepository,
        user_repo: UserRepository,
        refresh_repo: RefreshTokenRepository,
        audit: AuditRepository,
        session: AsyncSession,
    ) -> None:
        self.tenant_repo = tenant_repo
        self.user_repo = user_repo
        self.accounts = AccountRepository(session)
        self.refresh_repo = refresh_repo
        self.audit = audit
        self.session = session

    async def check_password(self, email: str, password: str) -> Account:
        """Первый шаг входа: почта и пароль. Сессию не открывает — дальше
        второй фактор или доверенное устройство (services/mfa_service.py).

        На все причины отказа — один ответ и одинаковое время: пароль
        проверяется и тогда, когда учётки нет (фиктивный хеш в
        verify_password). Иначе по времени ответа перебирались бы адреса.
        Неподтверждённая почта — отдельный ответ, но только после верного
        пароля: он ничего не говорит тому, кто пароля не знает.
        """
        account = await self.accounts.get_by_email(email)
        password_ok = verify_password(
            password, account.hashed_password if account else None
        )
        if account is None or not password_ok:
            reason = "unknown_account" if account is None else "wrong_password"
            logger.info("login_failed", reason=reason)
            # В журнал — и попытка по несуществующему адресу: серия таких
            # записей и есть след перебора. Коммитится только запись.
            self.audit.record(
                AuditAction.LOGIN_FAILED,
                details={
                    "reason": reason,
                    "email": email.strip().casefold(),
                    "account_id": str(account.id) if account else None,
                },
            )
            await self.session.commit()
            raise InvalidCredentialsError()

        if account.email_verified_at is None:
            raise EmailNotVerifiedError()
        if password_needs_rehash(account.hashed_password):
            account.hashed_password = hash_password(password)
        return account

    async def login_session(self, account: Account, *, remember: bool) -> TokenPair:
        """Второй шаг пройден — сессия в компании по умолчанию."""
        member = await self.default_membership(account)
        pair = await self.open_session(account, member, remember=remember)
        logger.info("login_succeeded", account_id=str(account.id))
        return pair

    async def open_session(
        self, account: Account, member: User | None, *, remember: bool = True
    ) -> TokenPair:
        """Новая сессия (вход, подтверждение почты, сброс пароля): пара
        токенов и коммит всей транзакции — действие и вход фиксируются
        вместе.

        Членство пишется в контексте своей компании: вне его RLS не
        найдёт строку, и UPDATE ничего не изменит (до 03.10 вход шёл
        всегда в контексте компании из кода).
        """
        now = _now()
        account.last_login_at = now
        scope = (
            tenant_scope(member.tenant_id)
            if member is not None
            else contextlib.nullcontext()
        )
        with scope:
            if member is not None:
                member.last_login_at = now
                account.last_tenant_id = member.tenant_id
            pair = await self._issue(
                account, member, family_id=uuid4(), remember=remember
            )
            self.audit.record(
                AuditAction.LOGIN_SUCCEEDED,
                tenant_id=member.tenant_id if member else None,
                actor_id=member.id if member else None,
                details={"account_id": str(account.id)},
            )
            await self.session.commit()
        return pair

    async def refresh(self, raw_token: str) -> TokenPair:
        """Обменять refresh-токен на новую пару (ротация).

        Предъявленный токен помечается использованным. Повторное
        предъявление использованного или отозванного токена — признак
        того, что его украли: отзывается вся цепочка этого входа.

        Компания сессии сохраняется, если членство в ней ещё действует;
        иначе (убрали, заблокировали, компанию приостановили) — сессия
        без компании, а не выход: учётка жива.
        """
        now = _now()
        record = await self.refresh_repo.get_by_hash(hash_refresh_token(raw_token))
        if record is None:
            raise NotAuthenticatedError("Невалидный refresh-токен")

        if record.used_at is not None or record.revoked_at is not None:
            await self.refresh_repo.revoke_family(record.family_id, now)
            self.audit.record(
                AuditAction.REFRESH_REUSE_DETECTED,
                tenant_id=record.tenant_id,
                actor_id=record.user_id,
                details={
                    "family_id": str(record.family_id),
                    "account_id": str(record.account_id),
                },
            )
            await self.session.commit()
            logger.warning(
                "refresh_token_reuse_detected",
                account_id=str(record.account_id),
                family_id=str(record.family_id),
            )
            raise NotAuthenticatedError("Невалидный refresh-токен")

        if record.expires_at <= now:
            raise NotAuthenticatedError("Refresh-токен истёк")

        account = await self.accounts.get(record.account_id)
        if account is None:
            await self.refresh_repo.revoke_family(record.family_id, now)
            await self.session.commit()
            raise NotAuthenticatedError("Учётная запись не найдена")

        member = None
        if record.tenant_id is not None and record.user_id is not None:
            member = await self.active_membership(account, record.tenant_id)

        record.used_at = now
        pair = await self._issue(
            account, member, family_id=record.family_id, remember=record.remember
        )
        await self.session.commit()
        return pair

    async def switch_company(
        self, account: Account, tenant_id: UUID | None, raw_token: str | None
    ) -> TokenPair:
        """Перейти в другую компанию (или выйти в «без компании»).

        Текущая цепочка refresh-токенов закрывается, начинается новая с
        выбранной компанией: старый refresh не вернёт прежнюю компанию.
        """
        member = None
        if tenant_id is not None:
            member = await self.active_membership(account, tenant_id)
            if member is None:
                raise NotFoundError("Компания не найдена")
        remember = True
        if raw_token is not None:
            record = await self.refresh_repo.get_by_hash(hash_refresh_token(raw_token))
            if record is not None and record.account_id == account.id:
                remember = record.remember
                await self.refresh_repo.revoke_family(record.family_id, _now())
        account.last_tenant_id = tenant_id
        pair = await self._issue(account, member, family_id=uuid4(), remember=remember)
        await self.session.commit()
        return pair

    async def logout(
        self,
        account: Account,
        member: User | None,
        session_id: UUID,
        raw_token: str | None,
    ) -> None:
        """Закрыть сеанс: цепочку refresh-токенов входа из access-токена
        (sid) — с ней перестаёт действовать и сам access-токен — и цепочку
        из cookie, если она другая (cookie от прежнего входа).

        Чужой или несуществующий refresh-токен молча игнорируется: ответ
        не должен подтверждать, что такой токен есть у другого человека.
        """
        now = _now()
        await self.refresh_repo.revoke_family(session_id, now)
        if raw_token is not None:
            record = await self.refresh_repo.get_by_hash(hash_refresh_token(raw_token))
            if (
                record is not None
                and record.account_id == account.id
                and record.family_id != session_id
            ):
                await self.refresh_repo.revoke_family(record.family_id, now)
        self.audit.record(
            AuditAction.LOGOUT,
            tenant_id=member.tenant_id if member else None,
            actor_id=member.id if member else None,
            details={"account_id": str(account.id)},
        )
        await self.session.commit()

    async def logout_everywhere(self, account: Account, member: User | None) -> None:
        """Выйти на всех устройствах: и refresh, и уже выданные access."""
        await self.invalidate_sessions(account)
        self.audit.record(
            AuditAction.LOGOUT_EVERYWHERE,
            tenant_id=member.tenant_id if member else None,
            actor_id=member.id if member else None,
            details={"account_id": str(account.id)},
        )
        await self.session.commit()
        logger.info("logout_everywhere", account_id=str(account.id))

    async def change_password(
        self,
        account: Account,
        member: User | None,
        current_password: str,
        new_password: str,
    ) -> TokenPair:
        """Сменить пароль и выдать новую пару токенов.

        Все прежние сессии закрываются: смену пароля чаще всего делают,
        когда подозревают, что пароль узнал кто-то ещё. На почту — письмо
        «пароль изменён» со ссылкой восстановления (ТЗ §3).
        """
        if not verify_password(current_password, account.hashed_password):
            raise InvalidCurrentPasswordError()
        if new_password == current_password:
            raise WeakPasswordError("Новый пароль совпадает с текущим")
        validate_password(new_password, email=account.email)

        set_password(account, new_password)
        account.must_change_password = False
        await self.invalidate_sessions(account)
        pair = await self._issue(account, member, family_id=uuid4(), remember=True)
        self.audit.record(
            AuditAction.PASSWORD_CHANGED,
            tenant_id=member.tenant_id if member else None,
            actor_id=member.id if member else None,
            details={"account_id": str(account.id)},
        )
        mail = EmailService(self.session)
        mail.enqueue(
            account.email,
            email_templates.password_changed(
                name=account.first_name, reset_url=mail.url("/forgot-password")
            ),
        )
        await self.session.commit()

        logger.info("password_changed", account_id=str(account.id))
        return pair

    async def invalidate_sessions(self, account: Account) -> None:
        """Все сессии и доверенные устройства учётки — прочь: смена пароля,
        «выйти везде», откат захвата почты."""
        account.token_version += 1
        await self.refresh_repo.revoke_account(account.id, _now())
        await self.session.execute(
            delete(TrustedDevice).where(TrustedDevice.account_id == account.id)
        )

    async def active_membership(self, account: Account, tenant_id: UUID) -> User | None:
        """Действующее членство учётки в компании или None."""
        tenant = await self.tenant_repo.get_by_id(tenant_id)
        if tenant is None or not tenant.is_active:
            return None
        with tenant_scope(tenant_id):
            member = await self.user_repo.get_by_account(account.id)
        if member is None or member.status is not MemberStatus.ACTIVE:
            return None
        return member

    async def default_membership(self, account: Account) -> User | None:
        """Компания после входа: последняя выбранная, если членство в ней
        действует, иначе первая действующая, иначе — без компании."""
        if account.last_tenant_id is not None:
            member = await self.active_membership(account, account.last_tenant_id)
            if member is not None:
                return member
        with account_scope(account.id):
            memberships = await self.user_repo.memberships_of_account(account.id)
        for candidate in memberships:
            if candidate.status is MemberStatus.ACTIVE:
                member = await self.active_membership(account, candidate.tenant_id)
                if member is not None:
                    return member
        return None

    async def _issue(
        self, account: Account, member: User | None, *, family_id: UUID, remember: bool
    ) -> TokenPair:
        settings = get_settings()
        raw = new_refresh_token()
        ttl = (
            timedelta(days=settings.refresh_token_ttl_days)
            if remember
            else timedelta(hours=settings.session_refresh_ttl_hours)
        )
        await self.refresh_repo.add(
            RefreshToken(
                account_id=account.id,
                user_id=member.id if member else None,
                tenant_id=member.tenant_id if member else None,
                family_id=family_id,
                token_hash=hash_refresh_token(raw),
                remember=remember,
                expires_at=_now() + ttl,
                user_agent=current_user_agent.get(),
                ip=current_client_ip.get(),
            )
        )
        access = create_access_token(
            account.id,
            account.token_version,
            session_id=family_id,
            tenant_id=member.tenant_id if member else None,
            member_id=member.id if member else None,
            role=member.role.value if member else None,
            member_version=member.token_version if member else None,
        )
        return TokenPair(
            access_token=access,
            refresh_token=raw,
            expires_in=settings.access_token_ttl_minutes * 60,
            remember=remember,
        )


def _now() -> datetime:
    return datetime.now(UTC)
