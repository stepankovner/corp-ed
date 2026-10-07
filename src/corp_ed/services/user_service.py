"""Люди компании — для администратора (ТЗ §2, §7).

Компания (тенант) берётся из контекста запроса — из подписанного токена
администратора. Параметра tenant_id нет ни в одном методе: тронуть
человека в чужой компании нельзя даже по ошибке.

Учёток админ не заводит и паролей не выдаёт (решение 03.10): люди
приходят по приглашению со своей учёткой kronto, пароль восстанавливают
по почте. Админ меняет роль, блокирует, одобряет вступивших, подтверждает
отдел, выбранный самим сотрудником, и убирает из компании.
"""

from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import (
    ConflictError,
    LastAdminError,
    NotFoundError,
    SelfModificationError,
)
from corp_ed.domain.models import Department, MemberStatus, User, UserRole
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.repositories.department_repository import DepartmentRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services.notification_service import (
    Notice,
    NotificationKind,
    NotificationService,
)
from corp_ed.services.seats import ensure_free_seat

logger = structlog.get_logger()

PROFILE_PATH = "/settings/profile"


class UserService:
    def __init__(
        self,
        repository: UserRepository,
        audit: AuditRepository,
        session: AsyncSession,
    ) -> None:
        self.repository = repository
        self.audit = audit
        self.session = session

    async def list_users(self) -> list[User]:
        return await self.repository.list_members()

    async def update_user(
        self,
        actor: User,
        user_id: UUID,
        *,
        role: UserRole | None = None,
        blocked: bool | None = None,
    ) -> User:
        """Сменить роль или заблокировать (разблокировать).

        Смена роли и блокировка отзывают токены этой компании у человека
        сразу (версия членства), его вход в другие компании не трогается.
        """
        user = await self._get(user_id)
        before = {"role": user.role.value, "status": user.status.value}

        new_status = user.status
        if blocked is True and user.status is MemberStatus.ACTIVE:
            new_status = MemberStatus.BLOCKED
        elif blocked is False and user.status is MemberStatus.BLOCKED:
            new_status = MemberStatus.ACTIVE
        changes_access = (role is not None and role is not user.role) or (
            new_status is not user.status
        )
        if changes_access and user.id == actor.id:
            raise SelfModificationError()

        loses_admin = user.role is UserRole.ADMIN and user.status is MemberStatus.ACTIVE
        loses_admin = loses_admin and (
            (role is not None and role is not UserRole.ADMIN)
            or new_status is not MemberStatus.ACTIVE
        )
        if loses_admin and await self.repository.count_active_admins() <= 1:
            raise LastAdminError()
        if new_status is MemberStatus.ACTIVE and user.status is MemberStatus.BLOCKED:
            # Разблокировка занимает место так же, как новый сотрудник.
            await ensure_free_seat(self.session, actor.tenant_id)

        if role is not None:
            user.role = role
        user.status = new_status
        if changes_access:
            user.token_version += 1

        self.audit.record(
            AuditAction.USER_UPDATED,
            tenant_id=user.tenant_id,
            actor_id=actor.id,
            target_type="user",
            target_id=user.id,
            details={
                "before": before,
                "after": {"role": user.role.value, "status": user.status.value},
            },
        )
        await self.session.commit()
        logger.info(
            "user_updated",
            user_id=str(user.id),
            actor_id=str(actor.id),
            role=user.role.value,
            status=user.status.value,
        )
        return user

    async def approve(self, actor: User, user_id: UUID) -> User:
        """Пустить вступившего по приглашению с одобрением."""
        user = await self._get(user_id)
        if user.status is not MemberStatus.PENDING:
            raise ConflictError("Заявка уже рассмотрена")
        await ensure_free_seat(self.session, actor.tenant_id)
        user.status = MemberStatus.ACTIVE
        user.token_version += 1
        self.audit.record(
            AuditAction.USER_APPROVED,
            tenant_id=user.tenant_id,
            actor_id=actor.id,
            target_type="user",
            target_id=user.id,
        )
        await self.session.commit()
        return user

    async def reject(self, actor: User, user_id: UUID) -> None:
        user = await self._get(user_id)
        if user.status is not MemberStatus.PENDING:
            raise ConflictError("Заявка уже рассмотрена")
        _mark_left(user)
        self.audit.record(
            AuditAction.USER_REJECTED,
            tenant_id=user.tenant_id,
            actor_id=actor.id,
            target_type="user",
            target_id=user.id,
        )
        await self.session.commit()

    async def confirm_department(
        self, actor: User, user_id: UUID, seen_department_id: UUID
    ) -> User:
        """Подтвердить отдел, выбранный сотрудником (ТЗ §7): с этого
        момента ему открыты закрытые папки отдела. Повтор — без изменений.
        seen_department_id — отдел, который видел администратор."""
        user, department = await self._with_department(user_id, seen_department_id)
        if user.department_confirmed:
            return user
        user.department_confirmed = True
        self._record_department(AuditAction.USER_DEPARTMENT_CONFIRMED, actor, user)
        NotificationService(self.session).notify_member(
            user,
            Notice(
                kind=NotificationKind.DEPARTMENT_CONFIRMED,
                title=f"Отдел подтверждён: {department.name}",
                lines=[
                    "Администратор подтвердил ваш отдел — вам открыты его "
                    "закрытые папки."
                ],
                link=PROFILE_PATH,
                action="Открыть профиль",
            ),
        )
        await self.session.commit()
        return user

    async def reject_department(
        self, actor: User, user_id: UUID, seen_department_id: UUID
    ) -> User:
        """Отклонить отдел, выбранный сотрудником: отдел снимается.
        Подтверждённый так не снять — его меняют в профиле сотрудника."""
        user, department = await self._with_department(user_id, seen_department_id)
        if user.department_confirmed:
            raise ConflictError(
                "Отдел уже подтверждён: изменить его можно в профиле сотрудника"
            )
        self._record_department(AuditAction.USER_DEPARTMENT_REJECTED, actor, user)
        user.department_id = None
        user.department_confirmed = False
        NotificationService(self.session).notify_member(
            user,
            Notice(
                kind=NotificationKind.DEPARTMENT_REJECTED,
                title=f"Отдел не подтверждён: {department.name}",
                lines=[
                    f"Администратор не подтвердил отдел «{department.name}». "
                    "Если это ошибка — уточните у администратора компании."
                ],
                link=PROFILE_PATH,
                action="Открыть профиль",
            ),
        )
        await self.session.commit()
        return user

    async def _with_department(
        self, user_id: UUID, seen_department_id: UUID
    ) -> tuple[User, Department]:
        """Человек и его отдел. Решение — только про тот отдел, что видел
        администратор: сотрудник мог сменить его, пока открыт список, и
        подтверждение «вслепую» открыло бы ему чужие закрытые папки."""
        user = await self._get(user_id)
        # Строка блокируется до конца решения: смена отдела сотрудником
        # подождёт, а не проскочит между проверкой и записью.
        await self.session.execute(
            select(User.id).where(User.id == user.id).with_for_update()
        )
        await self.session.refresh(
            user, attribute_names=["status", "department_id", "department_confirmed"]
        )
        if user.status is not MemberStatus.ACTIVE:
            raise NotFoundError("Пользователь не найден")
        department = (
            await DepartmentRepository(self.session).get_by_id(user.department_id)
            if user.department_id
            else None
        )
        if department is None:
            raise ConflictError("Сотрудник не выбрал отдел")
        if department.id != seen_department_id:
            raise ConflictError(
                "Сотрудник сменил отдел — обновите страницу и проверьте заново"
            )
        return user, department

    def _record_department(self, action: AuditAction, actor: User, user: User) -> None:
        self.audit.record(
            action,
            tenant_id=user.tenant_id,
            actor_id=actor.id,
            target_type="user",
            target_id=user.id,
            details={"department_id": str(user.department_id)},
        )

    async def remove(self, actor: User, user_id: UUID) -> None:
        """Убрать из компании: учётка человека остаётся, доступа к
        компании больше нет, его диалоги здесь скрываются (ТЗ §2)."""
        user = await self._get(user_id)
        if user.id == actor.id:
            raise SelfModificationError()
        if (
            user.role is UserRole.ADMIN
            and user.status is MemberStatus.ACTIVE
            and await self.repository.count_active_admins() <= 1
        ):
            raise LastAdminError()
        _mark_left(user)
        self.audit.record(
            AuditAction.USER_REMOVED,
            tenant_id=user.tenant_id,
            actor_id=actor.id,
            target_type="user",
            target_id=user.id,
        )
        await self.session.commit()
        logger.info("user_removed", user_id=str(user.id), actor_id=str(actor.id))

    async def _get(self, user_id: UUID) -> User:
        # Чужой пользователь не загрузится: хук изоляции добавит фильтр
        # по тенанту из контекста. Ответ — 404, а не 403, чтобы не
        # подтверждать, что такой id существует в другой компании.
        user = await self.repository.get_by_id(user_id)
        if user is None or user.status is MemberStatus.LEFT:
            raise NotFoundError("Пользователь не найден")
        return user


def _mark_left(user: User) -> None:
    user.status = MemberStatus.LEFT
    user.left_at = datetime.now(UTC)
    user.token_version += 1
