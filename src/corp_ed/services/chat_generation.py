"""Ответ в чате по мере генерации (ТЗ §6): фоновая задача и её события.

Почему задача, а не генератор ответа HTTP. Ответ пишется, даже если
сотрудник ушёл в другой диалог или закрыл вкладку: соединение рвётся, а
ответ дописывается и сохраняется, как в ChatGPT, — вернувшись, человек
его увидит. Поэтому у задачи своя сессия БД (сессия запроса закроется
раньше), а поток HTTP только пересылает её события из очереди.

Остановить ответ — отдельный запрос (POST …/stop): флаг в Redis, задача
проверяет его между кусками текста. Через Redis — потому что поток и
просьба остановить могут попасть в разные процессы API. Без Redis
(разработка, тесты) — память процесса.

Остановленный ответ — ответ: текст до остановки сохраняется, токены и
кредиты — по оценке. Сбой модели — сообщение с ошибкой и кнопкой
«Ответить заново»; что успело прийти, остаётся видно.
"""

import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.exceptions import CreditsExhaustedError
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import ChatMessage, User, UserRole
from corp_ed.domain.tokens import count_tokens
from corp_ed.domain.types import AnswerDiagnostics, AnswerOrigin, ChunkMatch
from corp_ed.llm.errors import LLMError
from corp_ed.llm.throttle import ThrottleBusyError
from corp_ed.prompts.dialogue import Turn
from corp_ed.repositories.chat_repository import AttachmentRepository, MessageRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services.chat_service import MessageView, SourceView, TurnStart
from corp_ed.services.faq_service import FaqService

logger = structlog.get_logger()

GENERATION_TIMEOUT = 300.0
"""Предел одного ответа целиком, с поиском и повторами провайдера."""

ATTACHMENT_CONTEXT_TOKENS = 6000
"""Бюджет выдержек вложений в промпте. Файл меньше — уходит целиком,
больше — ATTACHMENT_TOP_K ближайших к вопросу фрагментов."""
ATTACHMENT_TOP_K = 8


# --- события потока ----------------------------------------------------------


@dataclass(frozen=True)
class StageEvent:
    stage: str


@dataclass(frozen=True)
class OriginEvent:
    origin: AnswerOrigin


@dataclass(frozen=True)
class DeltaEvent:
    text: str


@dataclass(frozen=True)
class ResetEvent:
    pass


@dataclass(frozen=True)
class DoneEvent:
    answer: MessageView
    diagnostics: AnswerDiagnostics | None


@dataclass(frozen=True)
class ErrorEvent:
    code: str
    message: str
    answer: MessageView | None


ChatEvent = StageEvent | OriginEvent | DeltaEvent | ResetEvent | DoneEvent | ErrorEvent

ERROR_MESSAGES = {
    "credits_exhausted": (
        "Лимит обращений компании на этот месяц исчерпан. "
        "Обратитесь к администратору вашей компании"
    ),
    "llm_unavailable": "Сервис ответов временно недоступен. Попробуйте ещё раз",
    # Очередь к квоте модели переполнена (docs/LOAD-TEST.md): сервис жив,
    # вопросов больше, чем квота успевает, — это не сбой поставщика.
    "busy": "Сейчас очень много вопросов. Попробуйте через минуту",
    "timeout": "Ответ занял слишком много времени. Попробуйте ещё раз",
    "internal": "Не удалось получить ответ. Попробуйте ещё раз",
}


# --- «Остановить» --------------------------------------------------------------


class StopSignals(ABC):
    """Просьбы остановить ответ, общие для процессов API."""

    @abstractmethod
    async def request(self, message_id: UUID) -> None: ...

    @abstractmethod
    async def is_set(self, message_id: UUID) -> bool: ...


class InMemoryStopSignals(StopSignals):
    """Разработка и тесты: один процесс."""

    def __init__(self) -> None:
        self._stopped: set[UUID] = set()

    async def request(self, message_id: UUID) -> None:
        self._stopped.add(message_id)

    async def is_set(self, message_id: UUID) -> bool:
        return message_id in self._stopped


class RedisStopSignals(StopSignals):
    """Флаг в Redis на 10 минут. Проверка — не чаще раза в CHECK_INTERVAL:
    куски текста идут десятками в секунду. Redis недоступен — ответ
    дописывается (остановить не вышло, но и ломать нечего)."""

    TTL_SECONDS = 600
    CHECK_INTERVAL = 0.25

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._checked: dict[UUID, tuple[float, bool]] = {}

    @staticmethod
    def _key(message_id: UUID) -> str:
        return f"chat-stop:{message_id}"

    async def request(self, message_id: UUID) -> None:
        try:
            await self._redis.set(self._key(message_id), "1", ex=self.TTL_SECONDS)
        except RedisError:
            logger.warning("chat_stop_unavailable", stage="request")

    async def is_set(self, message_id: UUID) -> bool:
        now = time.monotonic()
        checked = self._checked.get(message_id)
        if checked is not None and now - checked[0] < self.CHECK_INTERVAL:
            return checked[1]
        try:
            value = bool(await self._redis.exists(self._key(message_id)))
        except RedisError:
            logger.warning("chat_stop_unavailable", stage="check")
            value = False
        self._checked[message_id] = (now, value)
        return value


# --- фоновые задачи -------------------------------------------------------------


class ChatRunner:
    """Задачи ответов этого процесса. Ссылки держим сами: задачу без
    ссылки сборщик мусора может прибить посреди ответа."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()

    def start(self, work: Awaitable[None]) -> asyncio.Task[None]:
        async def run() -> None:
            await work

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def shutdown(self, timeout: float = 15.0) -> None:
        """Остановка API: дать дописать начатые ответы, остальное прервать
        (такие сообщения станут «прерван» по STALE_GENERATION)."""
        if not self._tasks:
            return
        _, pending = await asyncio.wait(set(self._tasks), timeout=timeout)
        for task in pending:
            task.cancel()


# --- вложения в промпте -------------------------------------------------------------


class AttachmentContext:
    """Выдержки вложений диалога для промпта (AttachmentSource FaqService).

    Небольшие файлы целиком, по порядку; большие — ближайшие к вопросу
    фрагменты (их эмбеддинги посчитаны при загрузке), тоже по порядку
    текста. Вне порога расстояния: о файле спросили явно.
    """

    def __init__(self, session: AsyncSession, attachment_ids: list[UUID]) -> None:
        self.repo = AttachmentRepository(session)
        self.attachment_ids = attachment_ids
        self.selected: list[ChunkMatch] = []

    async def select(self, embedding: list[float]) -> list[ChunkMatch]:
        if not self.attachment_ids:
            return []
        attachments = {a.id: a for a in await self.repo.get_many(self.attachment_ids)}
        chunks = list(await self.repo.chunks_of(list(attachments)))
        order = {
            attachment_id: i for i, attachment_id in enumerate(self.attachment_ids)
        }
        chunks.sort(key=lambda c: (order.get(c.attachment_id, 0), c.position))
        total = sum(count_tokens(c.content) for c in chunks)
        distance: dict[UUID, float] = {}
        if total > ATTACHMENT_CONTEXT_TOKENS:
            nearest = await self.repo.nearest_chunks(
                list(attachments), embedding, ATTACHMENT_TOP_K
            )
            distance = {row[0]: float(row[1]) for row in nearest}
            chunks = [c for c in chunks if c.id in distance]
        selected: list[ChunkMatch] = []
        budget = ATTACHMENT_CONTEXT_TOKENS
        for chunk in chunks:
            tokens = count_tokens(chunk.content)
            if tokens > budget:
                break
            budget -= tokens
            attachment = attachments[chunk.attachment_id]
            selected.append(
                ChunkMatch(
                    id=chunk.id,
                    content=chunk.content,
                    material_id=attachment.id,
                    position=chunk.position,
                    distance=distance.get(chunk.id, 0.0),
                    title=attachment.filename,
                    heading_path=list(chunk.heading_path),
                    embed_text=chunk.embed_text,
                )
            )
        self.selected = selected
        return selected


# --- сама генерация -------------------------------------------------------------


class _StreamSink:
    """AnswerSink FaqService: события — в очередь потока HTTP."""

    def __init__(
        self, emit: Callable[[ChatEvent], None], stop: StopSignals, message_id: UUID
    ) -> None:
        self._emit = emit
        self._stop = stop
        self._message_id = message_id
        self.text = ""

    async def stage(self, stage: str) -> None:
        self._emit(StageEvent(stage))

    async def origin(self, origin: AnswerOrigin) -> None:
        self._emit(OriginEvent(origin))

    async def delta(self, text: str) -> None:
        self.text += text
        self._emit(DeltaEvent(text))

    async def reset(self) -> None:
        self.text = ""
        self._emit(ResetEvent())

    async def should_stop(self) -> bool:
        return await self._stop.is_set(self._message_id)


@dataclass(frozen=True)
class GenerationJob:
    tenant_id: UUID
    member_id: UUID
    conversation_id: UUID
    answer_id: UUID
    question: str
    history: list[Turn]
    attachment_ids: list[UUID]
    diagnostics: bool
    """Модель, токены и расстояние — только администратору (как /faq/ask)."""

    @classmethod
    def from_turn(cls, turn: TurnStart, member: User) -> "GenerationJob":
        return cls(
            tenant_id=member.tenant_id,
            member_id=member.id,
            conversation_id=turn.conversation.id,
            answer_id=turn.answer.message.id,
            question=turn.question_text,
            history=turn.history,
            attachment_ids=turn.attachment_ids,
            diagnostics=member.role is UserRole.ADMIN,
        )


class ChatGenerator:
    """Пишет ответ хода: FaqService с потоком и сохранение сообщения."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        build_faq: Callable[[AsyncSession], FaqService],
        stop: StopSignals,
    ) -> None:
        self.session_factory = session_factory
        self.build_faq = build_faq
        self.stop = stop

    async def run(self, job: GenerationJob, emit: Callable[[ChatEvent], None]) -> None:
        finished = False

        def relay(event: ChatEvent) -> None:
            nonlocal finished
            finished = finished or isinstance(event, DoneEvent | ErrorEvent)
            emit(event)

        try:
            with tenant_scope(job.tenant_id):
                async with self.session_factory() as session:
                    await self._run(session, job, relay)
        except Exception:
            # Не сохранились ни ответ, ни ошибка — например, база не отдала
            # соединение. Сообщение станет «прерван» по STALE_GENERATION.
            logger.exception("chat_answer_save_failed")
        finally:
            # Поток HTTP ждёт итога и до тех пор шлёт «пинг»: без него
            # сотрудник смотрел бы на «Ищу в документах» вечно
            # (docs/LOAD-TEST.md).
            if not finished:
                emit(ErrorEvent("internal", ERROR_MESSAGES["internal"], None))

    async def _run(
        self,
        session: AsyncSession,
        job: GenerationJob,
        emit: Callable[[ChatEvent], None],
    ) -> None:
        sink = _StreamSink(emit, self.stop, job.answer_id)
        attachments = AttachmentContext(session, job.attachment_ids)
        code: str | None = None
        try:
            member = await UserRepository(session).get_by_id(job.member_id)
            if member is None:
                raise LookupError("member is gone")
            faq = self.build_faq(session)
            async with asyncio.timeout(GENERATION_TIMEOUT):
                result = await faq.answer_turn(
                    job.question,
                    member,
                    history=job.history,
                    conversation_id=job.conversation_id,
                    sink=sink,
                    attachments=attachments,
                    commit=False,
                )
        except CreditsExhaustedError:
            code = "credits_exhausted"
        except ThrottleBusyError as exc:
            logger.warning("chat_answer_busy", error=str(exc))
            code = "busy"
        except LLMError as exc:
            logger.warning("chat_answer_llm_failed", error=str(exc))
            code = "llm_unavailable"
        except TimeoutError:
            logger.warning("chat_answer_timeout")
            code = "timeout"
        except Exception:
            logger.exception("chat_answer_failed")
            code = "internal"

        if code is not None:
            await session.rollback()
            message = await MessageRepository(session).get(job.answer_id)
            if message is not None:
                message.status = "failed"
                message.error_code = code
                message.content = sink.text.strip()
                await session.commit()
            emit(
                ErrorEvent(
                    code=code,
                    message=ERROR_MESSAGES[code],
                    answer=_view(message) if message is not None else None,
                )
            )
            return

        message = await MessageRepository(session).get(job.answer_id)
        if message is None:
            # Диалог удалили, пока писался ответ: журнал и кредиты — да,
            # сохранять нечего.
            await session.commit()
            emit(ErrorEvent("internal", ERROR_MESSAGES["internal"], None))
            return
        attached = {match.id for match in attachments.selected}
        message.content = result.content
        message.origin = result.origin.value
        message.sources = [
            _source_json(match, attachment=match.id in attached)
            for match in result.sources
        ]
        message.qa_log_id = result.log_id
        message.status = "stopped" if result.stopped else "complete"
        await session.commit()
        emit(
            DoneEvent(
                answer=_view(message),
                diagnostics=result.diagnostics if job.diagnostics else None,
            )
        )


def _source_json(match: ChunkMatch, *, attachment: bool) -> dict[str, object]:
    """Снимок выдержки в сообщении (ChatMessage.sources)."""
    return {
        "kind": "attachment" if attachment else "document",
        "chunk_id": str(match.id),
        "material_id": None if attachment else str(match.material_id),
        "attachment_id": str(match.material_id) if attachment else None,
        "title": match.title,
        "heading_path": list(match.heading_path),
        "position": match.position,
        "content": match.content,
        "source_url": match.source_url,
    }


def _view(message: ChatMessage) -> MessageView:
    """Ответ сразу после генерации: источники только что нашёл поиск с
    правами самого сотрудника — все доступны. Версии фронт уже знает из
    события start."""
    return MessageView(
        message=message,
        siblings=[],
        sources=[
            SourceView(
                kind=str(source["kind"]),
                title=str(source["title"]),
                heading_path=list(source["heading_path"]),
                position=int(source["position"]),
                content=str(source["content"]),
                source_url=source.get("source_url"),
                material_id=UUID(source["material_id"])
                if source.get("material_id")
                else None,
                attachment_id=UUID(source["attachment_id"])
                if source.get("attachment_id")
                else None,
            )
            for source in message.sources or []
        ],
    )
