from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, UploadFile, status

from corp_ed.api.v1.dependencies import get_material_service, require_role
from corp_ed.api.v1.rate_limits import (
    INGEST_PER_TENANT,
    UPLOAD_PER_TENANT,
    limit_by_tenant,
)
from corp_ed.api.v1.schemas.material import (
    MAX_TITLE_LENGTH,
    IngestResponse,
    MaterialCreateRequest,
    MaterialResponse,
    MaterialUpdateRequest,
)
from corp_ed.core.config import get_http_settings
from corp_ed.core.exceptions import UnacceptableFileError
from corp_ed.domain.models import User, UserRole
from corp_ed.services.material_service import MaterialService

router = APIRouter(prefix="/materials", tags=["materials"])

AdminUser = Annotated[User, Depends(require_role(UserRole.ADMIN))]


@router.get("", response_model=list[MaterialResponse])
async def list_materials(
    service: Annotated[MaterialService, Depends(get_material_service)],
    current_user: AdminUser,
) -> list[MaterialResponse]:
    materials = await service.list_all()
    return [MaterialResponse.model_validate(material) for material in materials]


@router.post(
    "",
    response_model=MaterialResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(limit_by_tenant(INGEST_PER_TENANT))],
)
async def create_material(
    data: MaterialCreateRequest,
    service: Annotated[MaterialService, Depends(get_material_service)],
    current_user: AdminUser,
) -> MaterialResponse:
    """Создать материал и сразу поставить его в очередь на индексацию."""
    material = await service.create(
        current_user, title=data.title, content=data.content
    )
    return MaterialResponse.model_validate(material)


@router.post(
    "/upload",
    response_model=MaterialResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(limit_by_tenant(UPLOAD_PER_TENANT))],
)
async def upload_material(
    file: Annotated[UploadFile, File(description="docx, pdf, txt или md")],
    title: Annotated[str, Form(min_length=1, max_length=MAX_TITLE_LENGTH)],
    service: Annotated[MaterialService, Depends(get_material_service)],
    current_user: AdminUser,
) -> MaterialResponse:
    """Загрузить документ файлом.

    title — человеческое название («Правила отбора в акселератор»), а не
    имя файла: оно уходит в крошки эмбеддинга и в подписи источников.
    Формат определяется по содержимому, а не по расширению и не по
    Content-Type, который присылает клиент.
    """
    limit = get_http_settings().max_upload_bytes
    # Тело уже ограничено middleware; повторная проверка — на случай,
    # если лимит там и здесь разойдутся.
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise UnacceptableFileError("document_too_large", "Файл слишком большой")
    material = await service.upload(
        current_user,
        title=title.strip(),
        filename=file.filename or "file",
        data=data,
    )
    return MaterialResponse.model_validate(material)


@router.get("/{material_id}", response_model=MaterialResponse)
async def get_material(
    material_id: UUID,
    service: Annotated[MaterialService, Depends(get_material_service)],
    current_user: AdminUser,
) -> MaterialResponse:
    return MaterialResponse.model_validate(await service.get(material_id))


@router.post(
    "/{material_id}/ingest",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(limit_by_tenant(INGEST_PER_TENANT))],
)
async def ingest_material(
    material_id: UUID,
    service: Annotated[MaterialService, Depends(get_material_service)],
    current_user: AdminUser,
) -> IngestResponse:
    """Переиндексировать: 202 сразу, работа — в воркере.

    Прогресс — по статусу в GET /materials/{id}.
    """
    material = await service.request_ingest(material_id)
    return IngestResponse(material_id=material.id, status=material.status)


@router.patch("/{material_id}", response_model=MaterialResponse)
async def update_material(
    material_id: UUID,
    data: MaterialUpdateRequest,
    service: Annotated[MaterialService, Depends(get_material_service)],
    current_user: AdminUser,
) -> MaterialResponse:
    """Переименовать. Материал встаёт в очередь на переиндексацию."""
    material = await service.rename(current_user, material_id, data.title)
    return MaterialResponse.model_validate(material)


@router.delete("/{material_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_material(
    material_id: UUID,
    service: Annotated[MaterialService, Depends(get_material_service)],
    current_user: AdminUser,
) -> None:
    await service.delete(current_user, material_id)
