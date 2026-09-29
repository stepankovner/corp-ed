"""Ограничение частоты запросов.

Две реализации одного контракта:
- RedisRateLimiter — для production: счётчики общие для всех воркеров
  и всех экземпляров API, переживают рестарт процесса;
- InMemoryRateLimiter — для разработки и тестов: без внешних сервисов,
  но у каждого процесса свой счётчик.

Алгоритм — фиксированное окно: INCR + EXPIRE на первом попадании.
Проще скользящего окна и токен-бакета, атомарен одной командой Lua.
Недостаток — на стыке окон возможен всплеск до 2×limit. Для защиты
от перебора паролей и расхода денег на LLM этого достаточно: важен
порядок величины, а не точность до запроса.
"""

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

from corp_ed.core.exceptions import DomainError


class RateLimitedError(DomainError):
    """Лимит исчерпан. HTTP 429 с заголовком Retry-After."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("Слишком много запросов, попробуйте позже")
        self.retry_after = max(retry_after, 1)


class RateLimiterUnavailableError(Exception):
    """Хранилище счётчиков недоступно (Redis лежит).

    Не DomainError: что делать — пропустить запрос или отказать —
    решает вызывающий по политике конкретной ручки (fail-open или
    fail-closed, см. api/v1/rate_limits.py).
    """


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    count: int
    retry_after: int
    """Секунд до конца окна (для Retry-After)."""


class RateLimiter(ABC):
    @abstractmethod
    async def hit(self, key: str, *, limit: int, window: int) -> RateDecision:
        """Засчитать попадание и сказать, укладывается ли оно в лимит."""

    @abstractmethod
    async def peek(self, key: str, *, limit: int) -> RateDecision:
        """Проверить без засчитывания (например, «заблокирован ли вход»)."""

    @abstractmethod
    async def reset(self, key: str) -> None:
        """Сбросить счётчик (успешный вход обнуляет неудачные попытки)."""


# Атомарно: второй воркер не увидит счётчик без TTL между INCR и EXPIRE.
_HIT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('TTL', KEYS[1])
return {count, ttl}
"""

KEY_PREFIX = "corp-ed:rl:"


class RedisRateLimiter(RateLimiter):
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._hit = redis.register_script(_HIT_SCRIPT)

    async def hit(self, key: str, *, limit: int, window: int) -> RateDecision:
        try:
            count, ttl = await self._hit(keys=[KEY_PREFIX + key], args=[window])
        except RedisError as exc:
            raise RateLimiterUnavailableError(str(exc)) from exc
        return RateDecision(
            allowed=int(count) <= limit,
            count=int(count),
            retry_after=max(int(ttl), 0),
        )

    async def peek(self, key: str, *, limit: int) -> RateDecision:
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.get(KEY_PREFIX + key)
                pipe.ttl(KEY_PREFIX + key)
                raw, ttl = await pipe.execute()
        except RedisError as exc:
            raise RateLimiterUnavailableError(str(exc)) from exc
        count = int(raw or 0)
        return RateDecision(
            allowed=count < limit, count=count, retry_after=max(int(ttl), 0)
        )

    async def reset(self, key: str) -> None:
        try:
            await self._redis.delete(KEY_PREFIX + key)
        except RedisError as exc:
            raise RateLimiterUnavailableError(str(exc)) from exc


class InMemoryRateLimiter(RateLimiter):
    """Счётчики в памяти процесса. Только разработка и тесты."""

    def __init__(self) -> None:
        self._clock = time.monotonic
        self._windows: dict[str, tuple[int, float]] = {}
        self._lock = asyncio.Lock()

    async def hit(self, key: str, *, limit: int, window: int) -> RateDecision:
        async with self._lock:
            now = self._clock()
            count, ends_at = self._windows.get(key, (0, now + window))
            if ends_at <= now:
                count, ends_at = 0, now + window
            count += 1
            self._windows[key] = (count, ends_at)
        return RateDecision(
            allowed=count <= limit, count=count, retry_after=int(ends_at - now)
        )

    async def peek(self, key: str, *, limit: int) -> RateDecision:
        async with self._lock:
            now = self._clock()
            count, ends_at = self._windows.get(key, (0, now))
            if ends_at <= now:
                count = 0
        return RateDecision(
            allowed=count < limit, count=count, retry_after=int(max(ends_at - now, 0))
        )

    async def reset(self, key: str) -> None:
        async with self._lock:
            self._windows.pop(key, None)
