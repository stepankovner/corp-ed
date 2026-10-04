"""Готовность сервиса целиком (GET /health/ready, П-9).

/health отвечает, пока жив процесс API. Сотруднику этого мало: без базы,
Redis или воркера ответа или индексации не будет. Эту проверку дёргают
внешний чекер (Ping-Admin — звонок ночью на боевом сервере) и blackbox в
Prometheus.

Воркер доказывает, что жив, ключом в Redis с коротким сроком: его
файл-пульс виден только внутри его контейнера.
"""

import asyncio
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from corp_ed.core.database import get_engine

WORKER_HEARTBEAT_KEY = "corp-ed:worker:heartbeat"
WORKER_HEARTBEAT_TTL = 120
"""Пульс пишется каждые 30 с; ключ живёт 2 минуты — пропуск одного-двух
пульсов не тревога."""

CHECK_TIMEOUT = 2.0


async def readiness_failures(state: Any) -> list[str]:
    """Имена упавших частей: database, redis, worker. Пусто — всё готово.

    Без Redis (разработка) проверяется только база: воркер тогда не
    пишет пульс в Redis, и отсутствие ключа — не сбой.
    """
    failed: list[str] = []
    try:
        async with asyncio.timeout(CHECK_TIMEOUT), get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError, TimeoutError):
        failed.append("database")

    redis: Redis | None = getattr(state, "redis", None)
    if redis is None:
        return failed
    try:
        async with asyncio.timeout(CHECK_TIMEOUT):
            await redis.ping()
            worker_alive = bool(await redis.exists(WORKER_HEARTBEAT_KEY))
    except (RedisError, OSError, TimeoutError):
        failed.append("redis")
        return failed
    if not worker_alive:
        failed.append("worker")
    return failed
