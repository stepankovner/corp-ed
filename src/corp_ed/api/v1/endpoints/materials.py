import asyncio
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
from corp_ed.domain.models import Material, User, UserRole
from corp_ed.ingest.extract import max_file_bytes
from corp_ed.services.material_service import MaterialService

router = APIRouter(prefix="/materials", tags=["materials"])

AdminUser = Annotated[User, Depends(require_role(UserRole.ADMIN))]

_large_uploads: asyncio.Semaphore | None = None


def _large_upload_slot(limit: int) -> asyncio.Semaphore:
    """Очередь больших загрузок в процессе: файл до 100 МБ лежит в памяти,
    пока идёт разбор, и копируется в процесс разбора (MAX_CONCURRENT_LARGE_UPLOADS)."""
    global _large_uploads
    if _large_uploads is None:
        _large_uploads = asyncio.Semaphore(limit)
    return _large_uploads


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
        current_user, title=data.title, content=data.content, folder_id=data.folder_id
    )
    return MaterialResponse.model_validate(material)


@router.post(
    "/upload",
    response_model=MaterialResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(limit_by_tenant(UPLOAD_PER_TENANT))],
)
async def upload_material(
    file: Annotated[
        UploadFile, File(description="docx, doc, xlsx, pptx, pdf, txt или md")
    ],
    title: Annotated[
        str, Form(min_length=1, max_length=MAX_TITLE_LENGTH, pattern=r"\S")
    ],
    service: Annotated[MaterialService, Depends(get_material_service)],
    current_user: AdminUser,
    folder_id: Annotated[UUID | None, Form()] = None,
) -> MaterialResponse:
    """Загрузить документ файлом (folder_id — сразу в папку, ТЗ §5).

    title — человеческое название («Правила отбора в акселератор»), а не
    имя файла: оно уходит в крошки эмбеддинга и в подписи источников.
    Формат определяется по содержимому, а не по расширению и не по
    Content-Type, который присылает клиент.
    """
    settings = get_http_settings()
    filename = file.filename or "file"
    # pdf, docx и pptx — до max_large_upload_bytes, остальное — до
    # max_upload_bytes. Тело уже ограничено middleware потолком; здесь —
    # лимит по формату.
    limit = max_file_bytes(
        filename,
        large=settings.max_large_upload_bytes,
        other=settings.max_upload_bytes,
    )
    if file.size is not None and file.size > limit:
        raise _too_large(limit)
    if file.size is not None and file.size <= settings.max_upload_bytes:
        material = await _store(
            service, current_user, file, limit, title, filename, folder_id
        )
    else:
        async with _large_upload_slot(settings.max_concurrent_large_uploads):
            material = await _store(
                service, current_user, file, limit, title, filename, folder_id
            )
    return MaterialResponse.model_validate(material)


def _too_large(limit: int) -> UnacceptableFileError:
    megabytes = limit // (1024 * 1024)
    return UnacceptableFileError("document_too_large", f"Файл больше {megabytes} МБ")


async def _store(
    service: MaterialService,
    actor: User,
    file: UploadFile,
    limit: int,
    title: str,
    filename: str,
    folder_id: UUID | None,
) -> Material:
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise _too_large(limit)
    return await service.upload(
        actor,
        title=title.strip(),
        filename=filename,
        data=data,
        folder_id=folder_id,
    )


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
    """Переименовать (материал встаёт в очередь на переиндексацию) или
    перенести в папку."""
    fields = data.model_dump(exclude_unset=True)
    material = await service.get(material_id)
    if data.title is not None:
        material = await service.rename(current_user, material_id, data.title)
    if "folder_id" in fields:
        material = await service.move(current_user, material_id, data.folder_id)
    return MaterialResponse.model_validate(material)


@router.delete("/{material_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_material(
    material_id: UUID,
    service: Annotated[MaterialService, Depends(get_material_service)],
    current_user: AdminUser,
) -> None:
    await service.delete(current_user, material_id)
