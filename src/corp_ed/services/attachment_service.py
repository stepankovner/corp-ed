"""Вложения к вопросу в чате (ТЗ §6): «спроси по этому договору».

Файл разбирается так же, как документ компании (песочница, те же
форматы), но в базу компании не попадает: ни в поиск коллег, ни в
«Документы». Хранится только текст по фрагментам — исходный файл нет — и
только пока жив диалог; не отправленное с вопросом удаляет purge через
сутки.

Небольшой файл (до ATTACHMENT_CONTEXT_TOKENS) уходит в промпт целиком,
эмбеддинги ему не нужны. У большого фрагменты считаются сразу при
загрузке — в долю квоты эмбеддингов ингеста, не вопросов, — и в промпт
идут ближайшие к вопросу.
"""

from uuid import UUID, uuid4

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import (
    CodedConflictError,
    NotFoundError,
    UnacceptableFileError,
)
from corp_ed.domain.models import ChatAttachment, ChatAttachmentChunk, User
from corp_ed.domain.split import split_document
from corp_ed.domain.tokens import count_tokens
from corp_ed.ingest.extract import ExtractionError, detect_format, error_message
from corp_ed.ingest.preprocess import preprocess
from corp_ed.ingest.sandbox import extract_isolated
from corp_ed.llm.embedding_gateway import EmbeddingGateway
from corp_ed.repositories.chat_repository import AttachmentRepository
from corp_ed.services.chat_generation import ATTACHMENT_CONTEXT_TOKENS

logger = structlog.get_logger()

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENT_CHUNKS = 80
"""Около 30 тысяч токенов — 40–50 страниц текста. Больше — в «Документы»."""
MAX_PENDING_ATTACHMENTS = 20
"""Загруженных, но ещё не отправленных с вопросом — на человека."""


class AttachmentService:
    def __init__(
        self,
        session: AsyncSession,
        embedding_gateway: EmbeddingGateway,
        *,
        chunk_tokens: int,
        overlap_tokens: int,
    ) -> None:
        self.session = session
        self.repo = AttachmentRepository(session)
        self.embedding_gateway = embedding_gateway
        self.chunk_tokens = chunk_tokens
        self.overlap_tokens = overlap_tokens

    async def upload(
        self, member: User, *, filename: str, data: bytes
    ) -> ChatAttachment:
        """Разобрать файл и сохранить его текст до отправки вопроса.

        Порядок — как у документов: дешёвые проверки, разбор в
        изолированном процессе (до 90 с, без открытой транзакции), потом
        эмбеддинги и база.
        """
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise UnacceptableFileError(
                "attachment_too_large", "Файл больше 10 МБ — выберите поменьше"
            )
        if await self.repo.count_pending(member.id) >= MAX_PENDING_ATTACHMENTS:
            raise CodedConflictError(
                "Слишком много неотправленных файлов — отправьте вопрос или "
                "уберите лишние",
                "attachments_pending_limit",
            )
        try:
            detected = detect_format(filename, data)
            markdown = await extract_isolated(detected.format, data)
        except ExtractionError as exc:
            logger.info("attachment_rejected", code=exc.code, size=len(data))
            raise UnacceptableFileError(
                exc.code, error_message(exc.code, filename)
            ) from exc

        drafts = split_document(
            preprocess(markdown),
            title=detected.filename,
            chunk_tokens=self.chunk_tokens,
            overlap_tokens=self.overlap_tokens,
        )
        if not drafts:
            raise UnacceptableFileError("no_text", error_message("no_text", filename))
        if len(drafts) > MAX_ATTACHMENT_CHUNKS:
            raise UnacceptableFileError(
                "attachment_too_large",
                "Файл слишком большой для вопроса: до 40–50 страниц текста. "
                "Большие документы администратор загружает в «Документы» компании",
            )
        tokens = sum(count_tokens(draft.llm_text) for draft in drafts)
        embed = tokens > ATTACHMENT_CONTEXT_TOKENS

        attachment = ChatAttachment(
            id=uuid4(),
            user_id=member.id,
            filename=detected.filename[:255],
            source_format=detected.format.value,
            size=len(data),
            tokens=tokens,
        )
        chunks: list[ChatAttachmentChunk] = []
        for draft in drafts:
            embedding = (
                (
                    await self.embedding_gateway.embed_document(draft.embed_text)
                ).embedding
                if embed
                else None
            )
            chunks.append(
                ChatAttachmentChunk(
                    attachment_id=attachment.id,
                    position=draft.position,
                    heading_path=draft.heading_path,
                    embed_text=draft.embed_text,
                    content=draft.llm_text,
                    embedding=embedding,
                )
            )
        self.session.add(attachment)
        await self.session.flush()
        self.session.add_all(chunks)
        await self.session.commit()
        logger.info(
            "attachment_uploaded",
            attachment_id=str(attachment.id),
            format=detected.format.value,
            size=len(data),
            chunks=len(chunks),
            embedded=embed,
        )
        return attachment

    async def delete_pending(self, member: User, attachment_id: UUID) -> None:
        """Убрать файл из ещё не отправленного вопроса. Отправленный живёт
        вместе с диалогом и удаляется с ним."""
        found = await self.repo.get_many([attachment_id])
        attachment = found[0] if found else None
        if (
            attachment is None
            or attachment.user_id != member.id
            or attachment.conversation_id is not None
        ):
            raise NotFoundError("Вложение не найдено")
        await self.repo.delete(attachment)
        await self.session.commit()
