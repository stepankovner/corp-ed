"""Память диалога (BH-28): последние реплики сотрудника — только в Redis.

Уточняющий вопрос («А для УМНИК?») понятен только вместе с прошлыми
репликами. Контракт ML брал их из qa_log, но ответ модели в журнале не
хранится (SECURITY.md §3.11): в нём выдержки из документов — зарплаты,
ФИО, внутренние условия. Решение Артёма 30.09: реплики живут в Redis, на
диск и в бэкапы не попадают (Redis в compose.yaml без сохранения на диск),
а через RAG_HISTORY_TTL_MINUTES после последнего вопроса диалога
удаляются сами. По умолчанию 12 часов: уточнение после обеда работает, на
следующий день диалог начинается заново.

Ключ — компания + сотрудник + диалог: прочитать чужой диалог нельзя, даже
подставив его conversation_id. Хранится то, что нужно prompts/dialogue.py:
вопрос после mask_pii (как в qa_log) и начало ответа, который видел
сотрудник.

Две реализации одного контракта, как у лимитов (core/rate_limit.py):
RedisDialogueStore — бой, InMemoryDialogueStore — разработка и тесты.
Сбой хранилища — не сбой ответа: FaqService отвечает без истории.
"""

import json
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

from corp_ed.prompts.dialogue import ANSWER_PREVIEW_CHARS, Turn

MAX_STORED_ANSWER_CHARS = 4 * ANSWER_PREVIEW_CHARS
"""Сколько символов ответа хранить. format_history сам убирает ссылки [n]
и режет до ANSWER_PREVIEW_CHARS; с запасом на ссылки и пробелы — больше
не нужно, а меньше данных — меньше риска."""


@dataclass(frozen=True)
class DialogueKey:
    tenant_id: UUID
    user_id: UUID
    conversation_id: UUID

    def redis_key(self) -> str:
        return f"dialogue:{self.tenant_id}:{self.user_id}:{self.conversation_id}"


class DialogueStoreUnavailableError(Exception):
    """Хранилище реплик недоступно (Redis лежит). Ответ идёт без истории."""


@dataclass(frozen=True)
class Remembered:
    """Реплика и документы, на которые опирался ответ: перед тем как дать
    историю модели, реплики по документам, к которым у сотрудника больше
    нет доступа, отбрасываются."""

    turn: Turn
    materials: frozenset[UUID] = field(default_factory=frozenset)


class DialogueStore(ABC):
    async def load(self, key: DialogueKey) -> list[Turn]:
        """Реплики диалога по порядку, старые первыми; нет — пусто."""
        return [item.turn for item in await self.load_remembered(key)]

    @abstractmethod
    async def load_remembered(self, key: DialogueKey) -> list[Remembered]:
        """Реплики с документами ответа, старые первыми; нет — пусто."""

    @abstractmethod
    async def append(
        self,
        key: DialogueKey,
        turn: Turn,
        *,
        keep: int,
        ttl_seconds: int,
        materials: Iterable[UUID] = (),
    ) -> None:
        """Добавить реплику, оставить последние keep и продлить срок жизни
        диалога до ttl_seconds от этого момента."""


def _encode(turn: Turn, materials: Iterable[UUID] = ()) -> str:
    return json.dumps(
        {
            "q": turn.question,
            "a": turn.answer[:MAX_STORED_ANSWER_CHARS],
            "m": sorted(str(m) for m in set(materials)),
        },
        ensure_ascii=False,
    )


def _decode(raw: bytes | str) -> Remembered | None:
    try:
        data = json.loads(raw)
        return Remembered(
            turn=Turn(question=str(data["q"]), answer=str(data["a"])),
            materials=frozenset(UUID(str(m)) for m in data.get("m") or []),
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


class RedisDialogueStore(DialogueStore):
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def load_remembered(self, key: DialogueKey) -> list[Remembered]:
        try:
            raw = await self._redis.lrange(key.redis_key(), 0, -1)
        except RedisError as exc:
            raise DialogueStoreUnavailableError from exc
        return [item for item in map(_decode, raw) if item is not None]

    async def append(
        self,
        key: DialogueKey,
        turn: Turn,
        *,
        keep: int,
        ttl_seconds: int,
        materials: Iterable[UUID] = (),
    ) -> None:
        name = key.redis_key()
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.rpush(name, _encode(turn, materials))
                pipe.ltrim(name, -keep, -1)
                pipe.expire(name, ttl_seconds)
                await pipe.execute()
        except RedisError as exc:
            raise DialogueStoreUnavailableError from exc


class InMemoryDialogueStore(DialogueStore):
    """Для разработки и тестов: у каждого процесса своя память."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._items: dict[str, tuple[float, list[str]]] = {}

    async def load_remembered(self, key: DialogueKey) -> list[Remembered]:
        name = key.redis_key()
        item = self._items.get(name)
        if item is None:
            return []
        expires_at, raw = item
        if expires_at <= self._clock():
            del self._items[name]
            return []
        return [entry for entry in map(_decode, raw) if entry is not None]

    async def append(
        self,
        key: DialogueKey,
        turn: Turn,
        *,
        keep: int,
        ttl_seconds: int,
        materials: Iterable[UUID] = (),
    ) -> None:
        name = key.redis_key()
        now = self._clock()
        item = self._items.get(name)
        raw = item[1] if item is not None and item[0] > now else []
        raw = [*raw, _encode(turn, materials)][-keep:]
        self._items[name] = (now + ttl_seconds, raw)
