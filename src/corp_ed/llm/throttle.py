"""Темп запросов к внешнему API с квотой (эмбеддинги Yandex AI Studio).

Не то же самое, что ограничение частоты (core/rate_limit.py): там
лишний запрос отклоняется, здесь — ждёт своей очереди. Квота
эмбеддингов — 10 запросов в секунду на каталог (замер ML 24.09), общая
для API (вопросы сотрудников) и воркера ингеста. Без общего темпа
параллельный ингест сразу ловит 429, а повторы «все разом» снова
упираются в квоту.

Алгоритм — слоты: каждый вызов acquire() занимает следующий свободный
момент времени и спит до него. Повторы после 429 тоже вызывают
acquire(), то есть тоже идут через слоты (образец — RateLimiter в
eval/yandex.py).

Поиск приоритетнее ингеста: у них разные ключи и свои доли квоты
(см. EmbeddingSettings), сотрудник не ждёт, пока воркер съест квоту.
"""

import asyncio
import time
from abc import ABC, abstractmethod

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from corp_ed.llm.errors import LLMError

logger = structlog.get_logger()


class Throttle(ABC):
    @abstractmethod
    async def acquire(self) -> None:
        """Дождаться своего слота или бросить LLMError, если ждать слишком долго."""


class ThrottleBusyError(LLMError):
    """Очередь к API длиннее допустимого ожидания — перегрузка."""

    def __init__(self, wait: float) -> None:
        super().__init__(f"throttle queue too long: {wait:.1f}s", retryable=True)


class InMemoryThrottle(Throttle):
    """Слоты в памяти процесса: разработка и тесты."""

    def __init__(self, rate: float, *, max_wait: float) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self._interval = 1.0 / rate
        self._max_wait = max_wait
        self._next_slot = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            wait = slot - now
            if wait > self._max_wait:
                raise ThrottleBusyError(wait)
            self._next_slot = slot + self._interval
        if wait > 0:
            await asyncio.sleep(wait)


# Время — от Redis (TIME), а не от процесса: у воркера и API часы могут
# расходиться, а слот должен быть один на всех. Возвращает ожидание в
# микросекундах; -1 — очередь длиннее max_wait, слот не занят.
_ACQUIRE_SCRIPT = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000000 + tonumber(t[2])
local interval = tonumber(ARGV[1])
local max_wait = tonumber(ARGV[2])
local next_slot = tonumber(redis.call('GET', KEYS[1]) or '0')
local slot = math.max(now, next_slot)
local wait = slot - now
if wait > max_wait then
  return -1
end
redis.call('SET', KEYS[1], tostring(slot + interval), 'PX', 60000)
return wait
"""

KEY_PREFIX = "corp-ed:throttle:"


class RedisThrottle(Throttle):
    """Слоты в Redis: один темп на все процессы API и воркеры."""

    def __init__(
        self, redis: Redis, name: str, rate: float, *, max_wait: float
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self._key = KEY_PREFIX + name
        self._interval_us = int(1_000_000 / rate)
        self._max_wait_us = int(max_wait * 1_000_000)
        self._max_wait = max_wait
        self._acquire = redis.register_script(_ACQUIRE_SCRIPT)

    async def acquire(self) -> None:
        try:
            wait_us = int(
                await self._acquire(
                    keys=[self._key], args=[self._interval_us, self._max_wait_us]
                )
            )
        except RedisError as exc:
            # Fail-open: без темпа запрос уйдёт, лишний 429 обработает
            # ретрай. Отказать сотруднику в ответе из-за счётчика — хуже.
            logger.error("throttle_unavailable", key=self._key, error=str(exc))
            return
        if wait_us < 0:
            raise ThrottleBusyError(self._max_wait)
        if wait_us > 0:
            await asyncio.sleep(wait_us / 1_000_000)
