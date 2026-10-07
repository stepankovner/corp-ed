"""Уведомления (ТЗ §8): колокольчик в приложении и письма.

Администратору — о том, что требует его действий: остановилось
подключение, лимит вопросов на 80 % и исчерпан, заявка на вступление,
отдел с закрытой папкой ждёт подтверждения, недельная сводка. Письма —
по его настройкам (строки настроек нет — все включены). Сотруднику
письма приходят только о безопасности (их шлют сервисы входа, здесь их
нет); в колокольчик — решение по его отделу (notify_member).

Сервис не коммитит в notify_admins: уведомление пишется в той же
транзакции, что и событие (порог кредитов, остановка подключения,
заявка), — нет события, нет и уведомления.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import (
    Account,
    MemberStatus,
    Notification,
    NotificationSetting,
    User,
    UserRole,
)
from corp_ed.services import email_templates
from corp_ed.services.email_service import EmailService

logger = structlog.get_logger()

INBOX_LIMIT = 30
SETTINGS_PATH = "/settings/notifications"


class NotificationKind(StrEnum):
    CONNECTOR_STOPPED = "connector_stopped"
    CREDITS_WARNING = "credits_warning"
    CREDITS_EXHAUSTED = "credits_exhausted"
    JOIN_REQUEST = "join_request"
    DEPARTMENT_REQUEST = "department_request"
    DEPARTMENT_CONFIRMED = "department_confirmed"
    DEPARTMENT_REJECTED = "department_rejected"
    WEEKLY_DIGEST = "weekly_digest"


_EMAIL_FLAG = {
    NotificationKind.CONNECTOR_STOPPED: "email_connectors",
    NotificationKind.CREDITS_WARNING: "email_credits",
    NotificationKind.CREDITS_EXHAUSTED: "email_credits",
    NotificationKind.JOIN_REQUEST: "email_join_requests",
    # Подтвердить отдел — тоже заявка: отдельной настройки писем не
    # заводим (ТЗ §8).
    NotificationKind.DEPARTMENT_REQUEST: "email_join_requests",
    NotificationKind.WEEKLY_DIGEST: "email_weekly_digest",
}

EMAIL_FLAGS = (
    "email_connectors",
    "email_credits",
    "email_join_requests",
    "email_weekly_digest",
)


@dataclass(frozen=True)
class Notice:
    kind: NotificationKind
    title: str
    lines: list[str]
    link: str
    """Путь на сайте: куда ведут колокольчик и кнопка в письме."""
    action: str
    """Подпись кнопки в письме."""


@dataclass(frozen=True)
class Inbox:
    items: list[Notification]
    unread: int


class NotificationService:
    def __init__(self, session: AsyncSession, mail: EmailService | None = None):
        self.session = session
        self.mail = mail or EmailService(session)

    async def notify_admins(self, tenant_id: UUID, notice: Notice) -> int:
        """Колокольчик каждому работающему администратору компании и
        письмо тем, у кого такие письма включены. Вызывать в контексте
        этой компании (tenant_scope или токен)."""
        rows = (
            await self.session.execute(
                select(User.id, Account.email, Account.first_name)
                .join(Account, Account.id == User.account_id)
                .where(
                    User.tenant_id == tenant_id,
                    User.role == UserRole.ADMIN,
                    User.status == MemberStatus.ACTIVE,
                )
            )
        ).all()
        if not rows:
            return 0
        settings = {
            item.user_id: item
            for item in (
                await self.session.scalars(
                    select(NotificationSetting).where(
                        NotificationSetting.tenant_id == tenant_id,
                        NotificationSetting.user_id.in_([row[0] for row in rows]),
                    )
                )
            ).all()
        }
        flag = _EMAIL_FLAG[notice.kind]
        for user_id, email, first_name in rows:
            self.session.add(
                Notification(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    kind=notice.kind.value,
                    title=notice.title[:200],
                    body="\n".join(notice.lines),
                    link=notice.link,
                )
            )
            setting = settings.get(user_id)
            if setting is None or getattr(setting, flag):
                self.mail.enqueue(
                    email,
                    email_templates.notice(
                        kind=notice.kind.value,
                        name=first_name,
                        title=notice.title,
                        lines=notice.lines,
                        url=self.mail.url(notice.link),
                        action=notice.action,
                        settings_url=self.mail.url(SETTINGS_PATH),
                    ),
                )
        logger.info(
            "admins_notified",
            tenant_id=str(tenant_id),
            kind=notice.kind.value,
            recipient_count=len(rows),
        )
        return len(rows)

    def notify_member(self, member: User, notice: Notice) -> None:
        """Колокольчик одному человеку, без письма: сотруднику письма —
        только о безопасности (ТЗ §8). В транзакции вызывающего."""
        self.session.add(
            Notification(
                tenant_id=member.tenant_id,
                user_id=member.id,
                kind=notice.kind.value,
                title=notice.title[:200],
                body="\n".join(notice.lines),
                link=notice.link,
            )
        )

    # --- колокольчик -----------------------------------------------------------

    async def inbox(self, member: User, limit: int = INBOX_LIMIT) -> Inbox:
        items = (
            await self.session.scalars(
                select(Notification)
                .where(Notification.user_id == member.id)
                .order_by(Notification.created_at.desc())
                .limit(limit)
            )
        ).all()
        unread = await self.session.scalar(
            select(func.count()).where(
                Notification.user_id == member.id, Notification.read_at.is_(None)
            )
        )
        return Inbox(items=list(items), unread=int(unread or 0))

    async def mark_read(self, member: User, ids: list[UUID] | None) -> None:
        """Прочитать перечисленные или все свои. Чужие id не трогает."""
        statement = (
            update(Notification)
            .where(Notification.user_id == member.id, Notification.read_at.is_(None))
            .values(read_at=datetime.now(UTC))
        )
        if ids is not None:
            statement = statement.where(Notification.id.in_(ids))
        await self.session.execute(statement)
        await self.session.commit()

    # --- настройки писем -------------------------------------------------------

    async def settings(self, member: User) -> dict[str, bool]:
        setting = await self.session.get(NotificationSetting, member.id)
        return {
            flag: getattr(setting, flag) if setting else True for flag in EMAIL_FLAGS
        }

    async def update_settings(
        self, member: User, changes: dict[str, bool]
    ) -> dict[str, bool]:
        setting = await self.session.get(NotificationSetting, member.id)
        if setting is None:
            setting = NotificationSetting(user_id=member.id)
            self.session.add(setting)
        for flag, value in changes.items():
            if flag in EMAIL_FLAGS:
                setattr(setting, flag, value)
        await self.session.commit()
        return await self.settings(member)
