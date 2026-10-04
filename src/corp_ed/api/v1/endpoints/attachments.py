"""Вложения к вопросу в чате (ТЗ §6). В базу компании не попадают."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, UploadFile, status

from corp_ed.api.v1.dependencies import get_attachment_service, get_current_user
from corp_ed.api.v1.rate_limits import ATTACHMENT_PER_USER, limit_by_user
from corp_ed.api.v1.schemas.chat import AttachmentResponse
from corp_ed.core.exceptions import UnacceptableFileError
from corp_ed.domain.models import User
from corp_ed.services.attachment_service import MAX_ATTACHMENT_BYTES, AttachmentService

router = APIRouter(prefix="/attachments", tags=["chat"])

Member = Annotated[User, Depends(get_current_user)]
Service = Annotated[AttachmentService, Depends(get_attachment_service)]


@router.post(
    "",
    response_model=AttachmentResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(limit_by_user(ATTACHMENT_PER_USER))],
)
async def upload_attachment(
    file: Annotated[
        UploadFile, File(description="docx, doc, xlsx, pptx, pdf, txt или md, до 10 МБ")
    ],
    service: Service,
    member: Member,
) -> AttachmentResponse:
    """Файл к следующему вопросу: id передать в attachment_ids вопроса.

    Хранится только текст файла и только вместе с диалогом; не
    отправленный с вопросом удаляется через сутки.
    """
    data = await file.read(MAX_ATTACHMENT_BYTES + 1)
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise UnacceptableFileError(
            "attachment_too_large", "Файл больше 10 МБ — выберите поменьше"
        )
    attachment = await service.upload(
        member, filename=file.filename or "file", data=data
    )
    return AttachmentResponse.of(attachment)


@router.delete("/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_attachment(
    attachment_id: UUID, service: Service, member: Member
) -> None:
    """Убрать файл из ещё не отправленного вопроса."""
    await service.delete_pending(member, attachment_id)
