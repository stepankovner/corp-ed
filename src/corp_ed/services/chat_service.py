"""Диалоги чата на сервере (ТЗ §6), как в Claude и ChatGPT.

Диалог — дерево сообщений: вопрос → ответ → вопрос… Правка вопроса
добавляет соседний вопрос (тот же родитель), «Ответить заново» — соседний
ответ; прежние ветки остаются, сотрудник переключает их стрелками.
current_message_id — лист показанной ветки; её и видит сотрудник, по ней
же собирается история для модели.

Свой диалог видит только сам человек. Администратор компании чужих
диалогов не видит ни здесь, ни где-либо ещё: у него обезличенная
статистика (ТЗ §6, «анонимность»). Поделиться можно ссылкой — её откроет
только коллега по той же компании, и только снимок ветки на момент, когда
поделились. Ссылка живёт CHAT_SHARE_TTL_DAYS дней, владелец её продлевает.

Сам ответ пишет фоновая задача (services/chat_generation.py): этот
сервис готовит ход (begin, begin_regenerate) и всё остальное.
"""

import secrets
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.dialogue_store import MAX_STORED_ANSWER_CHARS
from corp_ed.core.exceptions import CodedConflictError, ConflictError, NotFoundError
from corp_ed.domain.gaps import mask_pii
from corp_ed.domain.models import (
    ChatAttachment,
    ChatMessage,
    Conversation,
    MemberStatus,
    User,
)
from corp_ed.prompts.dialogue import Turn
from corp_ed.repositories.chat_repository import (
    AttachmentRepository,
    ConversationRepository,
    MessageRepository,
)
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.repositories.qa_log_repository import QaLogRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services.credit_service import CreditService

logger = structlog.get_logger()

MAX_QUESTION_CHARS = 4000
"""Вопрос до 4 000 символов (ТЗ §6; было 1 000)."""

MAX_TITLE_CHARS = 120
AUTO_TITLE_CHARS = 60
MAX_MESSAGES = 400
"""Сообщений в диалоге со всеми ветками: дальше — новый диалог."""
MAX_ATTACHMENTS_PER_QUESTION = 5
MAX_ATTACHMENTS_PER_CONVERSATION = 10
MAX_FEEDBACK_COMMENT = 1000

SHARE_TTL = timedelta(days=30)
"""Срок общей ссылки по умолчанию (CHAT_SHARE_TTL_DAYS)."""

STALE_GENERATION = timedelta(minutes=10)
"""Ответ «пишется» дольше — задача умерла (перезапуск API): он прерван.
Больше, чем предел самой генерации (chat_generation.GENERATION_TIMEOUT)."""

FEEDBACK_REASONS = frozenset(
    {"inaccurate", "incomplete", "outdated", "wrong_source", "other"}
)
"""Что не так с ответом (👎): неточно, неполно, устарело, не тот
документ, другое. Тексты кнопок — у фронта."""


class _Unset:
    pass


UNSET = _Unset()


@dataclass(frozen=True)
class SourceView:
    """Источник ответа для показа. content=None — документ удалён или
    недоступен смотрящему: видно только название."""

    kind: str
    title: str
    heading_path: list[str]
    position: int
    content: str | None
    source_url: str | None
    material_id: UUID | None
    attachment_id: UUID | None


@dataclass(frozen=True)
class MessageView:
    message: ChatMessage
    siblings: list[UUID]
    """Версии этого сообщения (ветки) по порядку, включая его самого."""
    attachments: list[ChatAttachment] = field(default_factory=list)
    sources: list[SourceView] = field(default_factory=list)
    content: str | None = None
    """Текст для этого смотрящего вместо сохранённого (общая ссылка: ответ
    по документу, к которому у него нет доступа); None — как сохранён."""


@dataclass(frozen=True)
class ConversationView:
    conversation: Conversation
    messages: list[MessageView]


@dataclass(frozen=True)
class SharedView:
    conversation: Conversation
    owner_name: str
    messages: list[MessageView]


@dataclass(frozen=True)
class TurnStart:
    """Готовый ход: что нужно фоновой задаче, чтобы написать ответ."""

    conversation: Conversation
    question: MessageView
    answer: MessageView
    question_text: str
    history: list[Turn]
    attachment_ids: list[UUID]


@dataclass(frozen=True)
class ConversationPage:
    items: list[Conversation]
    next_before: datetime | None


def make_title(question: str) -> str:
    """Название диалога по первому вопросу: первая строка, до 60 символов
    по границе слова."""
    line = (
        " ".join(question.strip().splitlines()[0].split()) if question.strip() else ""
    )
    if len(line) <= AUTO_TITLE_CHARS:
        return line or "Новый диалог"
    cut = line[:AUTO_TITLE_CHARS].rsplit(" ", 1)[0] or line[:AUTO_TITLE_CHARS]
    return cut.rstrip(" ,.;:—-") + "…"


def _now() -> datetime:
    return datetime.now(UTC)


class _Tree:
    """Сообщения диалога со всеми ветками."""

    def __init__(self, messages: Sequence[ChatMessage]) -> None:
        self.by_id = {m.id: m for m in messages}
        self.children: dict[UUID | None, list[ChatMessage]] = defaultdict(list)
        for message in messages:
            self.children[message.parent_id].append(message)

    def path(self, leaf_id: UUID | None) -> list[ChatMessage]:
        out: list[ChatMessage] = []
        current = self.by_id.get(leaf_id) if leaf_id else None
        while current is not None and len(out) <= len(self.by_id):
            out.append(current)
            current = self.by_id.get(current.parent_id) if current.parent_id else None
        return out[::-1]

    def siblings(self, message: ChatMessage) -> list[UUID]:
        return [m.id for m in self.children[message.parent_id]]

    def latest_leaf(self, message: ChatMessage) -> ChatMessage:
        """Самая свежая ветка под сообщением."""
        current = message
        while self.children[current.id]:
            current = self.children[current.id][-1]
        return current


class ChatService:
    def __init__(
        self,
        session: AsyncSession,
        credits: CreditService,
        *,
        history_turns: int,
        share_ttl: timedelta = SHARE_TTL,
    ) -> None:
        self.session = session
        self.conversations = ConversationRepository(session)
        self.messages = MessageRepository(session)
        self.attachments = AttachmentRepository(session)
        self.chunks = ChunkRepository(session)
        self.qa_log = QaLogRepository(session)
        self.users = UserRepository(session)
        self.credits = credits
        self.history_turns = history_turns
        self.share_ttl = share_ttl

    # --- список и карточка ---------------------------------------------------

    async def page(
        self,
        member: User,
        *,
        query: str | None = None,
        before: datetime | None = None,
        limit: int = 50,
    ) -> ConversationPage:
        """Закреплённые (на первой странице) и остальные по активности."""
        query = " ".join((query or "").split()) or None
        pinned = (
            list(
                await self.conversations.list_for(
                    member.id, query=query, pinned=True, before=None, limit=100
                )
            )
            if before is None
            else []
        )
        rest = list(
            await self.conversations.list_for(
                member.id, query=query, pinned=False, before=before, limit=limit + 1
            )
        )
        more = len(rest) > limit
        rest = rest[:limit]
        return ConversationPage(
            items=pinned + rest,
            next_before=rest[-1].updated_at if more and rest else None,
        )

    async def get(self, member: User, conversation_id: UUID) -> ConversationView:
        conversation = await self._own(member, conversation_id)
        tree = await self._tree(conversation)
        view = await self._view(conversation, tree, viewer=member)
        await self.session.commit()
        return view

    async def update(
        self,
        member: User,
        conversation_id: UUID,
        *,
        title: str | _Unset = UNSET,
        pinned: bool | _Unset = UNSET,
    ) -> Conversation:
        conversation = await self._own(member, conversation_id)
        if not isinstance(title, _Unset):
            cleaned = " ".join(title.split())[:MAX_TITLE_CHARS]
            if cleaned:
                conversation.title = cleaned
        if not isinstance(pinned, _Unset):
            if pinned and conversation.pinned_at is None:
                conversation.pinned_at = _now()
            elif not pinned:
                conversation.pinned_at = None
        await self.session.commit()
        return conversation

    async def delete(self, member: User, conversation_id: UUID) -> None:
        conversation = await self._own(member, conversation_id)
        await self.conversations.delete(conversation)
        await self.session.commit()
        logger.info("conversation_deleted", conversation_id=str(conversation_id))

    async def select(
        self, member: User, conversation_id: UUID, message_id: UUID
    ) -> ConversationView:
        """Показать другую версию сообщения: ветку под ней, самую свежую."""
        conversation = await self._own(member, conversation_id)
        tree = await self._tree(conversation)
        message = tree.by_id.get(message_id)
        if message is None:
            raise NotFoundError("Сообщение не найдено")
        conversation.current_message_id = tree.latest_leaf(message).id
        await self.session.commit()
        return await self._view(conversation, tree, viewer=member)

    # --- ход диалога ---------------------------------------------------------

    async def begin(
        self,
        member: User,
        *,
        conversation_id: UUID | None,
        parent_id: UUID | None,
        question: str,
        attachment_ids: Sequence[UUID] = (),
    ) -> TurnStart:
        """Новый вопрос: в новом диалоге, следом за ответом parent_id или,
        с parent_id другого ответа, — правка вопроса (новая ветка).

        Проверки — до потока, чтобы ошибки были обычными ответами HTTP:
        чужой диалог (404), ответ ещё пишется (409), пул кредитов
        исчерпан (402). Вопрос, пустой ответ «пишется» и выбор ветки
        фиксируются одной транзакцией.
        """
        text = question.strip()
        if not text or len(text) > MAX_QUESTION_CHARS:
            raise ConflictError(f"Вопрос — от 1 до {MAX_QUESTION_CHARS} символов")
        attachment_ids = list(dict.fromkeys(attachment_ids))
        if len(attachment_ids) > MAX_ATTACHMENTS_PER_QUESTION:
            raise ConflictError(
                f"К вопросу — не больше {MAX_ATTACHMENTS_PER_QUESTION} файлов"
            )
        await self.credits.ensure_available()

        if conversation_id is None:
            if parent_id is not None:
                raise NotFoundError("Сообщение не найдено")
            conversation = Conversation(
                id=uuid4(), user_id=member.id, title=make_title(text)
            )
            self.conversations.add(conversation)
            # Порядок вставки — явными flush: связей между моделями нет, и
            # unit of work не знает, что диалог нужен раньше сообщений.
            await self.session.flush()
            tree = _Tree([])
        else:
            conversation = await self._own(member, conversation_id, for_update=True)
            tree = await self._tree(conversation)
            self._check_can_write(tree)
            # parent_id=None в непустом диалоге — правка первого вопроса:
            # новая ветка от корня.
            if parent_id is not None:
                parent = tree.by_id.get(parent_id)
                if parent is None or parent.role != "assistant":
                    raise NotFoundError("Сообщение не найдено")

        attachments = await self._claim_attachments(
            member, conversation, attachment_ids
        )
        path = tree.path(parent_id)
        now = _now()
        question_message = ChatMessage(
            id=uuid4(),
            conversation_id=conversation.id,
            parent_id=parent_id,
            role="user",
            content=text,
            status="complete",
            attachment_ids=[a.id for a in attachments],
            created_at=now,
        )
        answer = ChatMessage(
            id=uuid4(),
            conversation_id=conversation.id,
            parent_id=question_message.id,
            role="assistant",
            content="",
            status="generating",
            created_at=now + timedelta(microseconds=1),
        )
        self.messages.add(question_message)
        await self.session.flush()
        self.messages.add(answer)
        conversation.current_message_id = answer.id
        conversation.updated_at = now
        await self.session.flush()
        tree = _Tree([*tree.by_id.values(), question_message, answer])
        start = TurnStart(
            conversation=conversation,
            question=MessageView(
                question_message, tree.siblings(question_message), attachments
            ),
            answer=MessageView(answer, tree.siblings(answer)),
            question_text=text,
            history=self._history(path, await self._closed_materials(path, member)),
            attachment_ids=self._path_attachments([*path, question_message]),
        )
        await self.session.commit()
        return start

    async def begin_regenerate(
        self, member: User, conversation_id: UUID, question_id: UUID
    ) -> TurnStart:
        """«Ответить заново»: новый ответ на тот же вопрос, прежний — ветка."""
        await self.credits.ensure_available()
        conversation = await self._own(member, conversation_id, for_update=True)
        tree = await self._tree(conversation)
        self._check_can_write(tree)
        question = tree.by_id.get(question_id)
        if question is None or question.role != "user":
            raise NotFoundError("Сообщение не найдено")
        path = tree.path(question.id)
        now = _now()
        answer = ChatMessage(
            id=uuid4(),
            conversation_id=conversation.id,
            parent_id=question.id,
            role="assistant",
            content="",
            status="generating",
            created_at=now,
        )
        self.messages.add(answer)
        conversation.current_message_id = answer.id
        conversation.updated_at = now
        await self.session.flush()
        tree = _Tree([*tree.by_id.values(), answer])
        attachments = await self.attachments.get_many(question.attachment_ids)
        start = TurnStart(
            conversation=conversation,
            question=MessageView(question, tree.siblings(question), attachments),
            answer=MessageView(answer, tree.siblings(answer)),
            question_text=question.content,
            history=self._history(
                path[:-1], await self._closed_materials(path, member)
            ),
            attachment_ids=self._path_attachments(path),
        )
        await self.session.commit()
        return start

    async def check_stop(
        self, member: User, conversation_id: UUID, message_id: UUID
    ) -> bool:
        """Можно ли остановить ответ: свой и ещё пишется. False — уже готов."""
        await self._own(member, conversation_id)
        message = await self.messages.get(message_id)
        if (
            message is None
            or message.conversation_id != conversation_id
            or message.role != "assistant"
        ):
            raise NotFoundError("Сообщение не найдено")
        return message.status == "generating"

    async def rate(
        self,
        member: User,
        conversation_id: UUID,
        message_id: UUID,
        *,
        value: int | None,
        reason: str | None,
        comment: str | None,
    ) -> ChatMessage:
        """👍/👎 с причиной и комментарием «что не так» (ТЗ §6).

        В журнал qa_log оценка идёт обезличенно: комментарий — после
        mask_pii, как сам вопрос. Его видят отчёт о пробелах и eval,
        администратор — без имени.
        """
        await self._own(member, conversation_id)
        message = await self.messages.get(message_id)
        if (
            message is None
            or message.conversation_id != conversation_id
            or message.role != "assistant"
            or message.status not in ("complete", "stopped")
        ):
            raise NotFoundError("Ответ не найден")
        if value is None:
            reason = comment = None
        if reason is not None and (value != -1 or reason not in FEEDBACK_REASONS):
            raise ConflictError("Причина — только к оценке «не помог»")
        comment = (comment or "").strip()[:MAX_FEEDBACK_COMMENT] or None
        message.feedback = value
        message.feedback_reason = reason
        message.feedback_comment = comment
        if message.qa_log_id is not None:
            entry = await self.qa_log.get_by_id(message.qa_log_id)
            if entry is not None:
                entry.feedback = value
                entry.feedback_reason = reason
                entry.feedback_comment = mask_pii(comment) if comment else None
        await self.session.commit()
        return message

    # --- поделиться ----------------------------------------------------------

    async def share(self, member: User, conversation_id: UUID) -> Conversation:
        """Ссылка для коллег по компании: снимок показанной ветки.

        Повторный вызов обновляет снимок и срок, ссылка та же. Отозвать —
        unshare: старая ссылка перестаёт открываться.
        """
        conversation = await self._own(member, conversation_id)
        if conversation.current_message_id is None:
            raise ConflictError("В диалоге ещё нет сообщений")
        if conversation.share_token is None:
            conversation.share_token = secrets.token_urlsafe(24)
        conversation.shared_message_id = conversation.current_message_id
        conversation.shared_at = _now()
        conversation.share_expires_at = conversation.shared_at + self.share_ttl
        await self.session.commit()
        return conversation

    async def renew_share(self, member: User, conversation_id: UUID) -> Conversation:
        """Продлить ссылку (и истёкшую): срок — от сегодня, токен и снимок
        прежние."""
        conversation = await self._own(member, conversation_id)
        if conversation.share_token is None:
            raise ConflictError("Ссылки на диалог нет — создайте её")
        conversation.share_expires_at = _now() + self.share_ttl
        await self.session.commit()
        return conversation

    async def unshare(self, member: User, conversation_id: UUID) -> None:
        conversation = await self._own(member, conversation_id)
        conversation.share_token = None
        conversation.shared_message_id = None
        conversation.shared_at = None
        conversation.share_expires_at = None
        await self.session.commit()

    async def shares(self, member: User) -> Sequence[Conversation]:
        """«Мои общие ссылки»: свои диалоги со ссылкой — и с истёкшей, её
        можно продлить. Свежие снимки — первыми."""
        return await self.conversations.list_shared(member.id)

    async def shared(self, viewer: User, token: str) -> SharedView:
        """Диалог по ссылке. Чужая компания ссылку не откроет (RLS и фильтр
        по компании), отозванная, истёкшая или от ушедшего коллеги — тоже
        404, неотличимо от несуществующей. Источники — по правам
        смотрящего, не автора."""
        conversation = await self.conversations.get_by_share_token(token)
        if (
            conversation is None
            or conversation.shared_message_id is None
            or not share_active(conversation)
        ):
            raise NotFoundError("Ссылка недействительна")
        owner = await self.users.get_by_id(conversation.user_id)
        if owner is None or owner.status is not MemberStatus.ACTIVE:
            raise NotFoundError("Ссылка недействительна")
        # Только чтение: зависшие ответы автора помечает его собственный
        # просмотр, не коллега по ссылке.
        tree = await self._tree(conversation, mark_stale=False)
        path = [
            m
            for m in tree.path(conversation.shared_message_id)
            if m.status != "generating"
        ]
        messages = [
            _hide_closed_answer(view)
            for view in await self._message_views(path, tree, viewer=viewer)
        ]
        return SharedView(
            conversation=conversation,
            owner_name=owner.full_name or "Коллега",
            messages=messages,
        )

    # --- внутреннее ----------------------------------------------------------

    async def _own(
        self, member: User, conversation_id: UUID, *, for_update: bool = False
    ) -> Conversation:
        """Свой диалог или 404 — чужой неотличим от несуществующего."""
        conversation = await self.conversations.get(
            conversation_id, for_update=for_update
        )
        if conversation is None or conversation.user_id != member.id:
            raise NotFoundError("Диалог не найден")
        return conversation

    async def _tree(
        self, conversation: Conversation, *, mark_stale: bool = True
    ) -> _Tree:
        messages = await self.messages.list_for(conversation.id)
        if not mark_stale:
            return _Tree(messages)
        # Задача, писавшая ответ, умерла (перезапуск API): ответ прерван.
        # Пишет вызывающий: своей транзакцией с остальными изменениями.
        stale = _now() - STALE_GENERATION
        for message in messages:
            if message.status == "generating" and message.created_at < stale:
                message.status = "failed"
                message.error_code = "interrupted"
        return _Tree(messages)

    def _check_can_write(self, tree: _Tree) -> None:
        if len(tree.by_id) + 2 > MAX_MESSAGES:
            raise CodedConflictError(
                "Диалог слишком длинный — начните новый", "conversation_full"
            )
        if any(m.status == "generating" for m in tree.by_id.values()):
            raise CodedConflictError(
                "Дождитесь ответа на предыдущий вопрос или остановите его",
                "answer_in_progress",
            )

    async def _claim_attachments(
        self, member: User, conversation: Conversation, ids: list[UUID]
    ) -> list[ChatAttachment]:
        """Вложения вопроса: свои, ещё не отправленные или из этого диалога."""
        attachments = await self.attachments.get_many(ids)
        if len(attachments) != len(ids) or any(
            a.user_id != member.id or a.conversation_id not in (None, conversation.id)
            for a in attachments
        ):
            raise NotFoundError("Вложение не найдено")
        fresh = [a for a in attachments if a.conversation_id is None]
        if fresh and (
            await self.attachments.count_in(conversation.id) + len(fresh)
            > MAX_ATTACHMENTS_PER_CONVERSATION
        ):
            raise CodedConflictError(
                f"В диалоге — не больше {MAX_ATTACHMENTS_PER_CONVERSATION} файлов",
                "attachments_limit",
            )
        for attachment in fresh:
            attachment.conversation_id = conversation.id
        return attachments

    def _history(self, path: Sequence[ChatMessage], closed: set[UUID]) -> list[Turn]:
        """Последние пары «вопрос — ответ» ветки для модели (BH-28).

        Вопрос — после mask_pii, как в журнале; ответ — что видел
        сотрудник, с той же обрезкой, что в Redis. Неудавшиеся ответы в
        историю не идут; ответы по документам, к которым у сотрудника
        больше нет доступа (closed), — тоже: иначе снятый доступ
        возвращался бы в модель через историю.
        """
        if self.history_turns <= 0:
            return []
        turns: list[Turn] = []
        for question, answer in zip(path, path[1:], strict=False):
            if (
                question.role == "user"
                and answer.role == "assistant"
                and answer.parent_id == question.id
                and answer.status in ("complete", "stopped")
                and answer.content
                and not _material_ids(answer) & closed
            ):
                turns.append(Turn(question=question.content, answer=answer.content))
        return [
            Turn(mask_pii(t.question), t.answer[:MAX_STORED_ANSWER_CHARS])
            for t in turns[-self.history_turns :]
        ]

    @staticmethod
    def _path_attachments(path: Sequence[ChatMessage]) -> list[UUID]:
        seen: dict[UUID, None] = {}
        for message in path:
            for attachment_id in message.attachment_ids or []:
                seen[attachment_id] = None
        return list(seen)

    async def _closed_materials(
        self, messages: Sequence[ChatMessage], viewer: User
    ) -> set[UUID]:
        ids = {i for message in messages for i in _material_ids(message)}
        return await self.chunks.closed_material_ids(ids, viewer=viewer.id)

    async def _view(
        self, conversation: Conversation, tree: _Tree, *, viewer: User
    ) -> ConversationView:
        path = tree.path(conversation.current_message_id)
        closed = await self._closed_materials(path, viewer)
        return ConversationView(
            conversation=conversation,
            messages=[
                _hide_answer(view)
                if _material_ids(view.message) & closed
                and view.message.role == "assistant"
                else view
                for view in await self._message_views(path, tree, viewer=viewer)
            ],
        )

    async def _message_views(
        self, path: Sequence[ChatMessage], tree: _Tree, *, viewer: User
    ) -> list[MessageView]:
        attachment_ids = self._path_attachments(path)
        attachments = {a.id: a for a in await self.attachments.get_many(attachment_ids)}
        material_ids = {
            UUID(source["material_id"])
            for message in path
            for source in message.sources or []
            if source.get("kind") == "document" and source.get("material_id")
        }
        visible = await self.chunks.visible_material_ids(material_ids, viewer=viewer.id)
        return [
            MessageView(
                message=message,
                siblings=tree.siblings(message),
                attachments=[
                    attachments[i]
                    for i in message.attachment_ids or []
                    if i in attachments
                ],
                sources=[source_view(s, visible) for s in message.sources or []],
            )
            for message in path
        ]


HIDDEN_ANSWER = "Ответ опирается на документы, к которым у вас нет доступа."


def _material_ids(message: ChatMessage) -> set[UUID]:
    return {
        UUID(source["material_id"])
        for source in message.sources or []
        if source.get("kind", "document") == "document" and source.get("material_id")
    }


def _hide_answer(view: MessageView) -> MessageView:
    return replace(view, content=HIDDEN_ANSWER)


def _hide_closed_answer(view: MessageView) -> MessageView:
    """Общая ссылка: текст источника без доступа скрыт, а ответ его
    пересказывает — скрыть и ответ (разбор 06.10). Свой диалог скрывается
    иначе (_view): только если доступ к документу снят, а не если документ
    удалён — смотрит тот же человек, что спрашивал."""
    if view.message.role != "assistant" or not any(
        source.kind == "document" and source.content is None for source in view.sources
    ):
        return view
    return _hide_answer(view)


def share_active(conversation: Conversation) -> bool:
    """Ссылка есть и срок не вышел. Без срока — не открывается."""
    expires = conversation.share_expires_at
    return (
        conversation.share_token is not None
        and expires is not None
        and expires > _now()
    )


def source_view(raw: dict[str, Any], visible: set[UUID]) -> SourceView:
    kind = str(raw.get("kind") or "document")
    material_id = UUID(raw["material_id"]) if raw.get("material_id") else None
    attachment_id = UUID(raw["attachment_id"]) if raw.get("attachment_id") else None
    available = kind == "attachment" or (
        material_id is not None and material_id in visible
    )
    return SourceView(
        kind=kind,
        title=str(raw.get("title") or ""),
        heading_path=[str(part) for part in raw.get("heading_path") or []],
        position=int(raw.get("position") or 0),
        content=str(raw.get("content") or "") if available else None,
        source_url=raw.get("source_url") if available else None,
        material_id=material_id,
        attachment_id=attachment_id,
    )
