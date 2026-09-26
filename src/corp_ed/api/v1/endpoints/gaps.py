from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from corp_ed.api.v1.dependencies import get_gap_service, require_role
from corp_ed.api.v1.schemas.gap import (
    GapClusterResponse,
    GapReportResponse,
    GapStatusRequest,
)
from corp_ed.domain.models import GapCluster, User, UserRole
from corp_ed.domain.types import GapStatus
from corp_ed.services.gap_service import GapService

router = APIRouter(prefix="/gaps", tags=["gaps"])

AdminUser = Annotated[User, Depends(require_role(UserRole.ADMIN))]
Service = Annotated[GapService, Depends(get_gap_service)]


def _response(cluster: GapCluster, samples: list[str]) -> GapClusterResponse:
    return GapClusterResponse(
        id=cluster.id,
        title=cluster.title,
        missing=cluster.missing,
        priority=cluster.priority,
        question_count=cluster.question_count,
        user_count=cluster.user_count,
        first_seen=cluster.first_seen,
        last_seen=cluster.last_seen,
        status=GapStatus(cluster.status),
        sample_questions=samples,
    )


@router.get("", response_model=GapReportResponse)
async def list_gaps(
    service: Service,
    current_user: AdminUser,
    status: GapStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> GapReportResponse:
    """Чего не хватает в документах компании — важное сверху (BH-23).

    Только пробелы (gap) и отказы модели при найденных выдержках
    (model_refusal); остальные промахи — внутренний мониторинг. Отчёт
    обновляется раз в сутки ночной задачей. Компания — из токена.
    """
    items = await service.report(status=status, limit=limit)
    return GapReportResponse(
        clusters=[_response(item.cluster, item.sample_questions) for item in items]
    )


@router.patch("/{cluster_id}", response_model=GapClusterResponse)
async def set_gap_status(
    cluster_id: UUID,
    data: GapStatusRequest,
    service: Service,
    current_user: AdminUser,
) -> GapClusterResponse:
    """Отметить пробел: в работе, закрыт (документ загружен), отклонён.

    Статус переживает ночную пересборку, пока группа узнаётся по общим
    вопросам.
    """
    item = await service.set_status(current_user, cluster_id, data.status)
    return _response(item.cluster, item.sample_questions)
