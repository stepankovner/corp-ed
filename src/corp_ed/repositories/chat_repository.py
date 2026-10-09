"""Диалоги, сообщения, вложения и подсказки чата (ТЗ §6).

Все таблицы — тенантные под RLS. ORM-выборки получают фильтр по
компании от хука изоляции; колоночные и массовые — явный, как везде.
Чей это диалог, проверяет сервис: в компании диалог видит только его
владелец (и те, с кем он поделился).
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import Row, any_, delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.tenant_context import require_tenant
from corp_ed.domain.models import (
    ChatAttachment,
    ChatAttachmentChunk,
    ChatMessage,
    ChatSuggestion,
    Chunk,
    Conversation,
    Material,
    QaLog,
)
from corp_ed.repositories.chunk_repository import visible_to


def like_pattern(query: str) -> str:
    """Подстрока для ILIKE: % и _ из запроса — буквально."""
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, conversation: Conversation) -> None:
        self.session.add(conversation)

    async def get(
        self, conversation_id: UUID, *, for_update: bool = False
    ) -> Conversation | None:
        stmt = select(Conversation).where(Conversation.id == conversation_id)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.scalars(stmt)).first()

    async def get_by_share_token(self, token: str) -> Conversation | None:
        stmt = select(Conversation).where(Conversation.share_token == token)
        return (await self.session.scalars(stmt)).first()

    async def list_shared(self, user_id: UUID) -> Sequence[Conversation]:
        stmt = (
            select(Conversation)
            .where(
                Conversation.user_id == user_id,
                Conversation.share_token.is_not(None),
            )
            .order_by(Conversation.shared_at.desc(), Conversation.id)
        )
        return (await self.session.scalars(stmt)).all()

    async def list_for(
        self,
        user_id: UUID,
        *,
        query: str | None,
        pinned: bool,
        before: datetime | None,
        limit: int,
    ) -> Sequence[Conversation]:
        """Диалоги человека: закреплённые — по времени закрепления, прочие —
        по последней активности. query — подстрока названия или текста
        вопросов и ответов."""
        stmt = select(Conversation).where(Conversation.user_id == user_id)
        if pinned:
            stmt = stmt.where(Conversation.pinned_at.is_not(None)).order_by(
                Conversation.pinned_at.desc(), Conversation.id.desc()
            )
        else:
            stmt = stmt.where(Conversation.pinned_at.is_(None)).order_by(
                Conversation.updated_at.desc(), Conversation.id.desc()
            )
            if before is not None:
                stmt = stmt.where(Conversation.updated_at < before)
        if query:
            pattern = like_pattern(query)
            stmt = stmt.where(
                or_(
                    Conversation.title.ilike(pattern, escape="\\"),
                    exists().where(
                        ChatMessage.conversation_id == Conversation.id,
                        ChatMessage.tenant_id == require_tenant(),
                        ChatMessage.content.ilike(pattern, escape="\\"),
                    ),
                )
            )
        return (await self.session.scalars(stmt.limit(limit))).all()

    async def delete(self, conversation: Conversation) -> None:
        # Сообщения, вложения и их фрагменты удаляет база (ON DELETE CASCADE).
        await self.session.delete(conversation)

    async def delete_inactive_before(self, cutoff: datetime) -> int:
        """Диалоги без активности с cutoff — целиком (purge).

        Активность — updated_at: его ставят только новый вопрос и «Ответить
        заново» (ChatService.begin, begin_regenerate), то есть это время
        последнего сообщения; переименование, закрепление, оценка и
        «поделиться» его не двигают, как и порядок в списке диалогов.
        Сообщения, вложения с фрагментами и общая ссылка (она в самой
        строке диалога) уходят вместе с ним.

        Вопрос, заданный в старом диалоге в ту же минуту, не оставит его
        наполовину удалённым: begin берёт строку FOR UPDATE, а DELETE с
        условием перепроверяет её после чужой транзакции. Либо вопрос
        успел — updated_at свежий и диалог остаётся, — либо диалог уже
        удалён и вопрос получает 404.
        """
        result = await self.session.execute(
            delete(Conversation).where(
                Conversation.tenant_id == require_tenant(),
                Conversation.updated_at < cutoff,
            )
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


class MessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, message: ChatMessage) -> None:
        self.session.add(message)

    async def get(self, message_id: UUID) -> ChatMessage | None:
        stmt = select(ChatMessage).where(ChatMessage.id == message_id)
        return (await self.session.scalars(stmt)).first()

    async def list_for(self, conversation_id: UUID) -> Sequence[ChatMessage]:
        """Все сообщения диалога, все ветки, в порядке создания."""
        stmt = (
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation_id)
            .order_by(ChatMessage.created_at, ChatMessage.id)
        )
        return (await self.session.scalars(stmt)).all()


class AttachmentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(
        self, attachment: ChatAttachment, chunks: list[ChatAttachmentChunk]
    ) -> None:
        self.session.add(attachment)
        self.session.add_all(chunks)

    async def get_many(self, ids: Sequence[UUID]) -> list[ChatAttachment]:
        if not ids:
            return []
        stmt = select(ChatAttachment).where(ChatAttachment.id.in_(ids))
        by_id = {item.id: item for item in (await self.session.scalars(stmt)).all()}
        return [by_id[i] for i in ids if i in by_id]

    async def count_pending(self, user_id: UUID) -> int:
        stmt = select(func.count()).where(
            ChatAttachment.tenant_id == require_tenant(),
            ChatAttachment.user_id == user_id,
            ChatAttachment.conversation_id.is_(None),
        )
        return int(await self.session.scalar(stmt) or 0)

    async def count_in(self, conversation_id: UUID) -> int:
        stmt = select(func.count()).where(
            ChatAttachment.tenant_id == require_tenant(),
            ChatAttachment.conversation_id == conversation_id,
        )
        return int(await self.session.scalar(stmt) or 0)

    async def delete(self, attachment: ChatAttachment) -> None:
        await self.session.delete(attachment)

    async def chunks_of(self, ids: Sequence[UUID]) -> Sequence[ChatAttachmentChunk]:
        """Фрагменты вложений по порядку: вложение за вложением, по позиции."""
        if not ids:
            return []
        stmt = (
            select(ChatAttachmentChunk)
            .where(ChatAttachmentChunk.attachment_id.in_(ids))
            .order_by(ChatAttachmentChunk.attachment_id, ChatAttachmentChunk.position)
        )
        return (await self.session.scalars(stmt)).all()

    async def nearest_chunks(
        self, ids: Sequence[UUID], embedding: list[float], limit: int
    ) -> Sequence[Row[tuple[UUID, float]]]:
        """Ближайшие к вопросу фрагменты вложений: (id, расстояние)."""
        distance = ChatAttachmentChunk.embedding.cosine_distance(embedding)
        stmt = (
            select(ChatAttachmentChunk.id, distance.label("distance"))
            .where(
                ChatAttachmentChunk.tenant_id == require_tenant(),
                ChatAttachmentChunk.attachment_id.in_(ids),
                ChatAttachmentChunk.embedding.is_not(None),
            )
            .order_by(distance)
            .limit(limit)
        )
        return (await self.session.execute(stmt)).all()

    async def delete_pending_older_than(self, cutoff: datetime) -> int:
        """Вложения, так и не отправленные с вопросом (purge)."""
        result = await self.session.execute(
            delete(ChatAttachment).where(
                ChatAttachment.tenant_id == require_tenant(),
                ChatAttachment.conversation_id.is_(None),
                ChatAttachment.created_at < cutoff,
            )
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


class SuggestionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, suggestion: ChatSuggestion) -> None:
        self.session.add(suggestion)

    async def list_all(self) -> list[ChatSuggestion]:
        stmt = select(ChatSuggestion).order_by(
            ChatSuggestion.position, ChatSuggestion.created_at
        )
        return list((await self.session.scalars(stmt)).all())

    async def get(self, suggestion_id: UUID) -> ChatSuggestion | None:
        stmt = select(ChatSuggestion).where(ChatSuggestion.id == suggestion_id)
        return (await self.session.scalars(stmt)).first()

    async def delete(self, suggestion: ChatSuggestion) -> None:
        await self.session.delete(suggestion)

    async def frequent_questions(
        self,
        *,
        viewer: UUID,
        since: datetime,
        min_users: int,
        limit: int,
        max_length: int,
    ) -> list[str]:
        """Частые вопросы компании, на которые документы ответили.

        Только обезличенно (ТЗ §6): вопрос из журнала (после mask_pii) и
        только если его задали не меньше min_users разных людей — один
        человек со своим вопросом в подсказки коллегам не попадает.
        Без вопросов-уточнений (в диалоге они непонятны вне его), без
        вопросов к вложениям, с маской персональных данных и с 👎.

        Только то, что смотрящий (viewer) мог бы узнать сам: запись
        журнала засчитывается, если среди выдержек её ответа есть фрагмент
        документа, который смотрящему сейчас виден (visible_to — то же
        правило, что в поиске), или выдержек из документов компании не
        было вовсе — тогда раскрывать нечего. Ответ только из закрытой
        папки, из документа с правами источника или из папки отдела,
        который ещё не подтвердил администратор, не засчитывается тому,
        кому они закрыты: формулировка вопроса выдала бы, о чём эти
        документы. Порог min_users и 👎 считаются по засчитанным записям.
        Документ удалён или переиндексирован (фрагментов с этими id
        больше нет) — запись тоже не засчитывается: лучше потерять
        подсказку, чем показать лишнее.
        """
        tenant_id = require_tenant()
        sources_visible = or_(
            func.cardinality(QaLog.source_chunk_ids) == 0,
            exists().where(
                Chunk.id == any_(QaLog.source_chunk_ids),
                Chunk.tenant_id == tenant_id,
                Material.id == Chunk.material_id,
                Material.tenant_id == tenant_id,
                visible_to(viewer, tenant_id),
            ),
        )
        normalized = func.lower(
            func.regexp_replace(
                func.regexp_replace(func.btrim(QaLog.question), r"\s+", " ", "g"),
                r"[?!.…\s]+$",
                "",
            )
        )
        users = func.count(func.distinct(QaLog.user_id))
        stmt = (
            select(func.mode().within_group(QaLog.question).label("sample"))
            .where(
                QaLog.tenant_id == tenant_id,
                QaLog.created_at >= since,
                QaLog.answer_given.is_(True),
                QaLog.history_turns == 0,
                QaLog.attachment_chunks == 0,
                func.length(QaLog.question) <= max_length,
                ~QaLog.question.contains("["),
                sources_visible,
            )
            .group_by(normalized)
            .having(users >= min_users, func.coalesce(func.min(QaLog.feedback), 0) >= 0)
            .order_by(users.desc(), func.count().desc())
            .limit(limit)
        )
        return [str(row.sample).strip() for row in await self.session.execute(stmt)]
