from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.request_context import current_client_ip, current_request_id
from corp_ed.domain.models import AuditEvent


class AuditAction(StrEnum):
    """Что попадает в журнал. Список закрытый: опечатка в строке
    события — это событие, которое никто никогда не найдёт поиском."""

    LOGIN_SUCCEEDED = "auth.login.succeeded"
    LOGIN_FAILED = "auth.login.failed"
    REFRESH_REUSE_DETECTED = "auth.refresh.reuse_detected"
    LOGOUT = "auth.logout"
    LOGOUT_EVERYWHERE = "auth.logout_everywhere"
    PASSWORD_CHANGED = "auth.password.changed"  # noqa: S105 — имя события
    USER_CREATED = "user.created"
    USER_UPDATED = "user.updated"
    USER_PASSWORD_RESET = "user.password_reset"  # noqa: S105 — имя события
    TENANT_CREATED = "tenant.created"
    TENANT_SUSPENDED = "tenant.suspended"
    TENANT_RESUMED = "tenant.resumed"
    MATERIAL_CREATED = "material.created"
    MATERIAL_UPDATED = "material.updated"
    MATERIAL_DELETED = "material.deleted"
    GLOSSARY_TERM_CREATED = "glossary.created"
    GLOSSARY_TERM_UPDATED = "glossary.updated"
    GLOSSARY_TERM_DELETED = "glossary.deleted"
    GAP_STATUS_CHANGED = "gap.status_changed"
    TENANT_SEATS_CHANGED = "tenant.seats_changed"
    TENANT_NOT_FOUND_MODE_CHANGED = "tenant.not_found_mode_changed"
    CREDITS_WARNING = "credits.warning"
    CREDITS_EXHAUSTED = "credits.exhausted"
    CONNECTOR_CREATED = "connector.created"
    CONNECTOR_UPDATED = "connector.updated"
    CONNECTOR_DELETED = "connector.deleted"
    CONNECTOR_CREDENTIALS_SET = "connector.credentials_set"  # noqa: S105 — имя события
    CONNECTOR_SYNC_REQUESTED = "connector.sync_requested"
    # Остановлен системой: источник отверг учётные данные.
    CONNECTOR_STOPPED = "connector.stopped"
    CONNECTOR_GRANT_SET = "connector.grant_set"
    CONNECTOR_GRANT_REVOKED = "connector.grant_revoked"
    CONNECTOR_GRANT_EXPIRED = "connector.grant_expired"


class AuditRepository:
    """Журнал аудита.

    record() только добавляет запись в сессию: коммит делает сервис
    вместе с самим действием. Действие и запись о нём фиксируются
    одной транзакцией — не бывает действия без записи и записи без
    действия. Исключение — неудачный вход: там коммитится только запись.

    Таблица не тенант-скоупная (см. AuditEvent), поэтому чтение
    фильтрует tenant_id явно.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def record(
        self,
        action: AuditAction,
        *,
        tenant_id: UUID | None,
        actor_id: UUID | None = None,
        target_type: str | None = None,
        target_id: UUID | str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.session.add(
            AuditEvent(
                tenant_id=tenant_id,
                actor_user_id=actor_id,
                action=action.value,
                target_type=target_type,
                target_id=str(target_id) if target_id is not None else None,
                ip=current_client_ip.get(),
                request_id=current_request_id.get(),
                details=details or {},
            )
        )

    async def exists_since(
        self, tenant_id: UUID, action: AuditAction, since: datetime
    ) -> bool:
        """Было ли такое событие у компании с момента since.

        Нужно событиям «раз за период» (порог кредитов): они не должны
        повторяться на каждый следующий вопрос.
        """
        result = await self.session.scalar(
            select(AuditEvent.id)
            .where(
                AuditEvent.tenant_id == tenant_id,
                AuditEvent.action == action.value,
                AuditEvent.created_at >= since,
            )
            .limit(1)
        )
        return result is not None

    async def list_for_tenant(
        self,
        tenant_id: UUID,
        *,
        limit: int,
        before: datetime | None = None,
    ) -> list[AuditEvent]:
        stmt = select(AuditEvent).where(AuditEvent.tenant_id == tenant_id)
        if before is not None:
            stmt = stmt.where(AuditEvent.created_at < before)
        stmt = stmt.order_by(AuditEvent.created_at.desc()).limit(limit)
        result = await self.session.scalars(stmt)
        return list(result)
