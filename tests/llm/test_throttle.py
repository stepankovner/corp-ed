"""Темп запросов к API эмбеддингов (квота 10 в секунду на каталог)."""

import asyncio
import os
from uuid import uuid4

import httpx
import pytest
from redis.asyncio import Redis

from corp_ed.core.config import EMBEDDING_DIM
from corp_ed.llm.throttle import (
    InMemoryThrottle,
    RedisThrottle,
    Throttle,
    ThrottleBusyError,
)
from corp_ed.llm.yandex_embedding import YandexEmbeddingAdapter


class Recorder:
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.sleeps.append(delay)


async def test_in_memory_throttle_spaces_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder()
    monkeypatch.setattr(asyncio, "sleep", recorder)
    throttle = InMemoryThrottle(rate=10, max_wait=5)

    for _ in range(3):
        await throttle.acquire()

    # Первый — сразу, дальше каждые ~0.1 с.
    assert len(recorder.sleeps) == 2
    assert recorder.sleeps[1] > recorder.sleeps[0] > 0.05


async def test_in_memory_throttle_refuses_long_queue() -> None:
    throttle = InMemoryThrottle(rate=1, max_wait=0.5)
    await throttle.acquire()

    with pytest.raises(ThrottleBusyError) as info:
        await throttle.acquire()
    assert info.value.retryable is True


def test_rate_must_be_positive() -> None:
    with pytest.raises(ValueError):
        InMemoryThrottle(rate=0, max_wait=1)


class CountingThrottle(Throttle):
    def __init__(self) -> None:
        self.acquired = 0

    async def acquire(self) -> None:
        self.acquired += 1


async def test_every_attempt_takes_a_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Повтор после 429 тоже идёт через темп — иначе снова упрётся в квоту."""

    async def no_sleep(_: float) -> None:
        pass

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    responses = [
        httpx.Response(429, json={}),
        httpx.Response(
            200,
            json={
                "embedding": [0.1] * EMBEDDING_DIM,
                "numTokens": "5",
                "modelVersion": "v",
            },
        ),
    ]
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: responses.pop(0))
    )
    documents, queries = CountingThrottle(), CountingThrottle()
    adapter = YandexEmbeddingAdapter(
        client=client,
        folder_id="f",
        api_key="k",
        base_delay=0,
        document_throttle=documents,
        query_throttle=queries,
    )

    await adapter.embed_document("текст")

    assert documents.acquired == 2
    assert queries.acquired == 0


REDIS_URL = os.environ.get("TEST_REDIS_URL")


@pytest.mark.skipif(REDIS_URL is None, reason="TEST_REDIS_URL не задан (в CI задан)")
async def test_redis_throttle_is_shared_between_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Два экземпляра (API и воркер) делят одни слоты."""
    assert REDIS_URL is not None
    redis = Redis.from_url(REDIS_URL)
    name = f"test-{uuid4()}"
    recorder = Recorder()
    try:
        api = RedisThrottle(redis, name, rate=10, max_wait=5)
        worker = RedisThrottle(redis, name, rate=10, max_wait=5)
        monkeypatch.setattr(asyncio, "sleep", recorder)

        await api.acquire()
        await worker.acquire()

        assert len(recorder.sleeps) == 1
        assert 0.05 < recorder.sleeps[0] <= 0.1
    finally:
        await redis.aclose()


async def test_redis_throttle_fails_open() -> None:
    """Redis лежит — запрос уходит без темпа, лишний 429 обработает ретрай."""
    redis = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    try:
        await RedisThrottle(redis, "x", rate=1, max_wait=1).acquire()
    finally:
        await redis.aclose()
