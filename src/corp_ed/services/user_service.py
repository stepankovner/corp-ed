from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import (
    EmailAlreadyExistsError,
    LastAdminError,
    NotFoundError,
    SelfModificationError,
)
from corp_ed.core.password_policy import validate_password
from corp_ed.core.security import generate_temporary_password, hash_password
from corp_ed.domain.models import User, UserRole
from corp_ed.repositories.refresh_token_repository import RefreshTokenRepository
from corp_ed.repositories.user_repository import UserRepository

logger = structlog.get_logger()


@dataclass(frozen=True)
class CreatedUser:
    user: User
    temporary_password: str | None
    """Сгенерированный пароль — показывается один раз и нигде не хранится."""


class UserService:
    """Управление пользователями компании. Вызывает только ADMIN.

    Компания (тенант) берётся из контекста запроса — из подписанного
    токена администратора. Параметра tenant_id нет ни в одном методе:
    завести пользователя в чужой компании нельзя даже по ошибке.
    """

    def __init__(
        self,
        repository: UserRepository,
        refresh_repo: RefreshTokenRepository,
        session: AsyncSession,
    ) -> None:
        self.repository = repository
        self.refresh_repo = refresh_repo
        self.session = session

    async def list_users(self) -> list[User]:
        return await self.repository.list_all()

    async def create_user(
        self,
        *,
        email: str,
        full_name: str | None,
        role: UserRole,
        password: str | None,
    ) -> CreatedUser:
        """Завести сотрудника.

        Пароль задаёт администратор или генерирует система. В обоих
        случаях его знает не только владелец, поэтому при первом входе
        его обязательно сменить (must_change_password).
        """
        email = email.casefold()
        if await self.repository.get_by_email(email) is not None:
            raise EmailAlreadyExistsError(email)

        temporary = None
        if password is None:
            temporary = password = generate_temporary_password()
        else:
            validate_password(password, email=email)

        user = await self.repository.create(
            User(
                email=email,
                full_name=full_name,
                role=role,
                hashed_password=hash_password(password),
                must_change_password=True,
            )
        )
        await self.session.commit()

        logger.info("user_created", user_id=str(user.id), role=role.value)
        return CreatedUser(user=user, temporary_password=temporary)

    async def update_user(
        self,
        actor: User,
        user_id: UUID,
        *,
        role: UserRole | None = None,
        is_active: bool | None = None,
        full_name: str | None = None,
    ) -> User:
        """Сменить роль, заблокировать или переименовать сотрудника.

        Смена роли и блокировка закрывают все сессии пользователя сразу,
        не дожидаясь истечения access-токена.
        """
        user = await self._get(user_id)

        changes_access = (role is not None and role is not user.role) or (
            is_active is not None and is_active is not user.is_active
        )
        if changes_access and user.id == actor.id:
            raise SelfModificationError()

        loses_admin = user.role is UserRole.ADMIN and user.is_active
        loses_admin = loses_admin and (
            (role is not None and role is not UserRole.ADMIN) or is_active is False
        )
        if loses_admin and await self.repository.count_active_admins() <= 1:
            raise LastAdminError()

        if role is not None:
            user.role = role
        if is_active is not None:
            user.is_active = is_active
        if full_name is not None:
            user.full_name = full_name

        if changes_access:
            user.token_version += 1
            await self.refresh_repo.revoke_user(user.id, datetime.now(UTC))

        await self.session.commit()
        logger.info(
            "user_updated",
            user_id=str(user.id),
            actor_id=str(actor.id),
            role=user.role.value,
            is_active=user.is_active,
        )
        return user

    async def reset_password(self, actor: User, user_id: UUID) -> str:
        """Выдать новый временный пароль и закрыть все сессии пользователя.

        Пароль генерирует система: администратор не придумывает его сам,
        а значит, не использует один и тот же для всех.
        """
        user = await self._get(user_id)
        temporary = generate_temporary_password()

        user.hashed_password = hash_password(temporary)
        user.must_change_password = True
        user.token_version += 1
        await self.refresh_repo.revoke_user(user.id, datetime.now(UTC))
        await self.session.commit()

        logger.info("password_reset", user_id=str(user.id), actor_id=str(actor.id))
        return temporary

    async def _get(self, user_id: UUID) -> User:
        # Чужой пользователь не загрузится: хук изоляции добавит фильтр
        # по тенанту из контекста. Ответ — 404, а не 403, чтобы не
        # подтверждать, что такой id существует в другой компании.
        user = await self.repository.get_by_id(user_id)
        if user is None:
            raise NotFoundError("Пользователь не найден")
        return user
