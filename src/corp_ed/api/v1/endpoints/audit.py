from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from corp_ed.api.v1.dependencies import get_audit_repository, require_role
from corp_ed.api.v1.schemas.audit import AuditEventResponse
from corp_ed.domain.models import User, UserRole
from corp_ed.repositories.audit_repository import AuditRepository

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=list[AuditEventResponse])
async def list_audit_events(
    audit: Annotated[AuditRepository, Depends(get_audit_repository)],
    current_user: Annotated[User, Depends(require_role(UserRole.ADMIN))],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: datetime | None = None,
) -> list[AuditEventResponse]:
    """Журнал своей компании, новые сверху. Постранично: before —
    created_at последней записи предыдущей страницы.

    Компания — только из токена. Параметра tenant_id нет: чужой журнал
    не запросить даже по ошибке.
    """
    events = await audit.list_for_tenant(
        current_user.tenant_id, limit=limit, before=before
    )
    return [AuditEventResponse.model_validate(event) for event in events]
