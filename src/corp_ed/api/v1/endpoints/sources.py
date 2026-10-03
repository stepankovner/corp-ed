"""«Где ищет ассистент» для сотрудника (ТЗ §5)."""

from typing import Annotated

from fastapi import APIRouter, Depends

from corp_ed.api.v1.dependencies import get_current_user, get_sources_service
from corp_ed.api.v1.schemas.company import (
    ConnectorSourceResponse,
    FileGroupResponse,
    MySourcesResponse,
)
from corp_ed.domain.models import User
from corp_ed.services.sources_service import SourcesService

router = APIRouter(prefix="/sources", tags=["connectors"])


@router.get("/mine", response_model=MySourcesResponse)
async def my_sources(
    service: Annotated[SourcesService, Depends(get_sources_service)],
    member: Annotated[User, Depends(get_current_user)],
) -> MySourcesResponse:
    """Загруженные документы, которые видит сотрудник (по папкам), и
    источники компании с его личным подключением, где оно нужно."""
    sources = await service.mine(member)
    return MySourcesResponse(
        files=[
            FileGroupResponse(
                folder_id=group.folder_id,
                name=group.name,
                restricted=group.restricted,
                documents=group.documents,
            )
            for group in sources.files
        ],
        connectors=[
            ConnectorSourceResponse(
                id=c.id,
                kind=c.kind,
                name=c.name,
                mode=c.mode,
                working=c.working,
                grant_status=c.grant_status,
            )
            for c in sources.connectors
        ],
    )
