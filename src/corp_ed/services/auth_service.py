from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.config import get_settings
from corp_ed.core.exceptions import (
    DomainError,
    InvalidCredentialsError,
    NotAuthenticatedError,
    WeakPasswordError,
)
from corp_ed.core.password_policy import validate_password
from corp_ed.core.security import (
    create_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    password_needs_rehash,
    verify_password,
)
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import RefreshToken, User
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.repositories.refresh_token_repository import RefreshTokenRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.repositories.user_repository import UserRepository

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


class AuthService:
    """Вход, обновление и отзыв токенов, смена пароля."""

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
        self.refresh_repo = refresh_repo
        self.audit = audit
        self.session = session

    async def login(self, company_code: str, email: str, password: str) -> TokenPair:
        """Проверить учётные данные и выдать пару токенов.

        На все причины отказа — один ответ и одинаковое время: пароль
        проверяется даже тогда, когда компании или пользователя нет
        (фиктивный хеш в verify_password). Иначе по времени ответа
        перебирались бы существующие компании и адреса.

        Тенант выставляется только на время поиска пользователя и
        сбрасывается на выходе (tenant_scope): раньше контекст ставился
        из непроверенного company_code и оставался после метода.
        """
        tenant = await self.tenant_repo.get_by_company_code(company_code)
        user: User | None = None
        if tenant is not None and tenant.is_active:
            with tenant_scope(tenant.id):
                user = await self.user_repo.get_by_email(email)

        password_ok = verify_password(password, user.hashed_password if user else None)

        if user is None or not password_ok or not user.is_active:
            reason = _failure_reason(tenant is not None, user, password_ok)
            logger.info(
                "login_failed",
                tenant_id=str(tenant.id) if tenant else None,
                reason=reason,
            )
            # В журнал — и попытка по несуществующему адресу: серия таких
            # записей и есть след перебора. Коммитится только запись.
            self.audit.record(
                AuditAction.LOGIN_FAILED,
                tenant_id=tenant.id if tenant else None,
                actor_id=user.id if user else None,
                details={
                    "reason": reason,
                    "company_code": company_code.casefold(),
                    "email": email.casefold(),
                },
            )
            await self.session.commit()
            raise InvalidCredentialsError()

        with tenant_scope(user.tenant_id):
            if password_needs_rehash(user.hashed_password):
                user.hashed_password = hash_password(password)
            user.last_login_at = _now()
            pair = await self._issue(user, family_id=uuid4())
            self.audit.record(
                AuditAction.LOGIN_SUCCEEDED, tenant_id=user.tenant_id, actor_id=user.id
            )
            await self.session.commit()

        logger.info(
            "login_succeeded", user_id=str(user.id), tenant_id=str(user.tenant_id)
        )
        return pair

    async def refresh(self, raw_token: str) -> TokenPair:
        """Обменять refresh-токен на новую пару (ротация).

        Предъявленный токен помечается использованным. Повторное
        предъявление использованного или отозванного токена — признак
        того, что его украли: отзывается вся цепочка этого входа, и
        выйти придётся и злоумышленнику, и владельцу.
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
                details={"family_id": str(record.family_id)},
            )
            await self.session.commit()
            logger.warning(
                "refresh_token_reuse_detected",
                user_id=str(record.user_id),
                tenant_id=str(record.tenant_id),
                family_id=str(record.family_id),
            )
            raise NotAuthenticatedError("Невалидный refresh-токен")

        if record.expires_at <= now:
            raise NotAuthenticatedError("Refresh-токен истёк")

        with tenant_scope(record.tenant_id):
            tenant = await self.tenant_repo.get_by_id(record.tenant_id)
            user = await self.user_repo.get_by_id(record.user_id)
            if (
                tenant is None
                or not tenant.is_active
                or user is None
                or not user.is_active
            ):
                await self.refresh_repo.revoke_family(record.family_id, now)
                await self.session.commit()
                raise NotAuthenticatedError("Пользователь не найден или неактивен")

            record.used_at = now
            pair = await self._issue(user, family_id=record.family_id)
            await self.session.commit()

        return pair

    async def logout(self, user: User, raw_token: str) -> None:
        """Отозвать цепочку refresh-токенов текущего входа.

        Чужой или несуществующий токен молча игнорируется: ответ не
        должен подтверждать, что такой токен есть у другого человека.
        """
        record = await self.refresh_repo.get_by_hash(hash_refresh_token(raw_token))
        if record is not None and record.user_id == user.id:
            await self.refresh_repo.revoke_family(record.family_id, _now())
        self.audit.record(
            AuditAction.LOGOUT, tenant_id=user.tenant_id, actor_id=user.id
        )
        await self.session.commit()

    async def logout_everywhere(self, user: User) -> None:
        """Выйти на всех устройствах: и refresh, и уже выданные access."""
        await self._invalidate_sessions(user)
        self.audit.record(
            AuditAction.LOGOUT_EVERYWHERE, tenant_id=user.tenant_id, actor_id=user.id
        )
        await self.session.commit()
        logger.info("logout_everywhere", user_id=str(user.id))

    async def change_password(
        self, user: User, current_password: str, new_password: str
    ) -> TokenPair:
        """Сменить пароль и выдать новую пару токенов.

        Все прежние сессии пользователя закрываются: смену пароля чаще
        всего делают, когда подозревают, что пароль узнал кто-то ещё.
        """
        if not verify_password(current_password, user.hashed_password):
            raise InvalidCurrentPasswordError()
        if new_password == current_password:
            raise WeakPasswordError("Новый пароль совпадает с текущим")
        validate_password(new_password, email=user.email)

        user.hashed_password = hash_password(new_password)
        user.must_change_password = False
        await self._invalidate_sessions(user)
        pair = await self._issue(user, family_id=uuid4())
        self.audit.record(
            AuditAction.PASSWORD_CHANGED, tenant_id=user.tenant_id, actor_id=user.id
        )
        await self.session.commit()

        logger.info("password_changed", user_id=str(user.id))
        return pair

    async def _invalidate_sessions(self, user: User) -> None:
        user.token_version += 1
        await self.refresh_repo.revoke_user(user.id, _now())

    async def _issue(self, user: User, family_id: UUID) -> TokenPair:
        settings = get_settings()
        raw = new_refresh_token()
        await self.refresh_repo.add(
            RefreshToken(
                user_id=user.id,
                tenant_id=user.tenant_id,
                family_id=family_id,
                token_hash=hash_refresh_token(raw),
                expires_at=_now() + timedelta(days=settings.refresh_token_ttl_days),
            )
        )
        access = create_access_token(
            user_id=user.id,
            tenant_id=user.tenant_id,
            role=user.role.value,
            token_version=user.token_version,
        )
        return TokenPair(
            access_token=access,
            refresh_token=raw,
            expires_in=settings.access_token_ttl_minutes * 60,
        )


def _failure_reason(tenant_found: bool, user: User | None, password_ok: bool) -> str:
    """Причина отказа — только в лог, клиенту всегда один ответ."""
    if not tenant_found:
        return "unknown_or_inactive_company"
    if user is None:
        return "unknown_user"
    if not password_ok:
        return "wrong_password"
    return "inactive_user"


def _now() -> datetime:
    return datetime.now(UTC)
