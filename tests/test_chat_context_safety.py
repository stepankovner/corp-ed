"""Standalone chat regressions: real selection/cache logic, no DB or network.

Run with --noconftest to avoid the integration database fixture.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from redis.exceptions import RedisError

from corp_ed.domain.tokens import count_tokens
from corp_ed.llm.errors import LLMError
from corp_ed.llm.types import EmbeddingResult
from corp_ed.services import chat_generation
from corp_ed.services.chat_generation import (
    ATTACHMENT_CONTEXT_TOKENS,
    ATTACHMENT_TOP_K,
    AttachmentContext,
    RedisStopSignals,
)


def context_for(documents):
    """Nearest query excludes NULL vectors, like AttachmentRepository SQL."""
    session = SimpleNamespace(
        in_transaction=Mock(return_value=True), commit=AsyncMock(), flush=AsyncMock()
    )
    attachments, chunks = [], []
    for tokens, embedded in documents:
        attachment = SimpleNamespace(id=uuid4(), filename="file.txt")
        attachments.append(attachment)
        for position in range(tokens // 500):
            chunks.append(
                SimpleNamespace(
                    id=uuid4(),
                    attachment_id=attachment.id,
                    position=position,
                    content="x" * 1500,
                    heading_path=[],
                    embed_text=f"{attachment.id}:{position}",
                    embedding=[0.0, 1.0] if embedded else None,
                )
            )

    async def embed(text):
        # The connection must be returned before the external request.
        session.commit.assert_awaited_once()
        return EmbeddingResult([1.0, 0.0], 500, "fake", "fake", 0)

    async def nearest(ids, query, limit):
        candidates = [chunk for chunk in chunks if chunk.embedding is not None]
        candidates.sort(key=lambda chunk: (-chunk.embedding[0], chunk.position))
        return [(chunk.id, 1 - chunk.embedding[0]) for chunk in candidates[:limit]]

    gateway = SimpleNamespace(embed_document=AsyncMock(side_effect=embed))
    context = AttachmentContext(
        session, [attachment.id for attachment in attachments], gateway
    )
    context.repo = SimpleNamespace(
        get_many=AsyncMock(return_value=attachments),
        chunks_of=AsyncMock(return_value=chunks),
        nearest_chunks=AsyncMock(side_effect=nearest),
    )
    return context, gateway, session, chunks


@pytest.mark.parametrize(
    "documents", [[(4000, False), (4000, False)], [(8000, True), (1000, False)]]
)
async def test_combined_attachments_index_missing_vectors_before_search(documents):
    context, gateway, session, chunks = context_for(documents)
    missing = [chunk for chunk in chunks if chunk.embedding is None]

    selected = await context.select([1.0, 0.0])

    assert selected
    assert {chunk.id for chunk in missing} & {match.id for match in selected}
    assert gateway.embed_document.await_count == len(missing)
    assert all(chunk.embedding is not None for chunk in chunks)
    assert len(selected) <= ATTACHMENT_TOP_K
    assert (
        sum(count_tokens(match.content) for match in selected)
        <= ATTACHMENT_CONTEXT_TOKENS
    )
    assert context.selected == selected
    session.flush.assert_awaited_once()

    # Follow-up: stored vectors are reused, rather than paid for again.
    await context.select([1.0, 0.0])
    assert gateway.embed_document.await_count == len(missing)


@pytest.mark.parametrize(
    "documents",
    [[(1000, False), (1000, False)], [(3000, False), (3000, False)], [(8000, True)]],
)
async def test_no_new_embeddings_when_context_fits_or_vectors_exist(documents):
    context, gateway, session, _ = context_for(documents)

    assert await context.select([1.0, 0.0])

    gateway.embed_document.assert_not_awaited()
    session.commit.assert_not_awaited()
    session.flush.assert_not_awaited()


async def test_embedding_failure_does_not_silently_drop_attachments():
    context, gateway, session, _ = context_for([(4000, False), (4000, False)])
    gateway.embed_document.side_effect = LLMError("provider down", retryable=True)

    with pytest.raises(LLMError, match="provider down"):
        await context.select([1.0, 0.0])

    context.repo.nearest_chunks.assert_not_awaited()
    session.flush.assert_not_awaited()
    assert context.selected == []


async def test_cancelled_indexing_does_not_flush_partial_vectors():
    context, gateway, session, chunks = context_for([(4000, False), (4000, False)])
    gateway.embed_document.side_effect = [
        EmbeddingResult([1.0, 0.0], 500, "fake", "fake", 0),
        asyncio.CancelledError(),
    ]

    with pytest.raises(asyncio.CancelledError):
        await context.select([1.0, 0.0])

    assert all(chunk.embedding is None for chunk in chunks)
    session.flush.assert_not_awaited()


@pytest.fixture
def stop_cache(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(chat_generation.time, "monotonic", lambda: clock.now)
    redis = SimpleNamespace(exists=AsyncMock(return_value=0), set=AsyncMock())
    return RedisStopSignals(redis), redis, clock


async def test_stop_polling_rechecks_after_interval(stop_cache):
    signals, redis, clock = stop_cache
    message_id = uuid4()
    assert not await signals.is_set(message_id)
    redis.exists.return_value = 1
    assert not await signals.is_set(message_id)
    redis.exists.assert_awaited_once()
    clock.now += signals.CHECK_INTERVAL + 0.01
    assert await signals.is_set(message_id)
    assert redis.exists.await_count == 2


async def test_stop_cache_expires_unrelated_completed_messages(stop_cache):
    signals, _, clock = stop_cache
    for _ in range(100):
        await signals.is_set(uuid4())
    clock.now += signals.CHECK_INTERVAL + 0.01
    active = uuid4()
    await signals.is_set(active)
    assert list(signals._checked) == [active]


async def test_stop_cache_has_a_hard_bound_during_a_burst(stop_cache):
    signals, redis, _ = stop_cache
    first_message = uuid4()
    await signals.is_set(first_message)
    for _ in range(2000):
        await signals.is_set(uuid4())
    assert len(signals._checked) <= 1024
    redis.exists.return_value = 1
    assert await signals.is_set(first_message)


async def test_own_stop_request_invalidates_cached_false(stop_cache):
    signals, redis, _ = stop_cache
    message_id = uuid4()
    assert not await signals.is_set(message_id)
    await signals.request(message_id)
    redis.exists.return_value = 1
    assert await signals.is_set(message_id)
    redis.set.assert_awaited_once_with(signals._key(message_id), "1", ex=600)


async def test_redis_failure_allows_answer_then_retries(stop_cache):
    signals, redis, clock = stop_cache
    message_id = uuid4()
    redis.exists.side_effect = RedisError("offline")
    assert not await signals.is_set(message_id)
    clock.now += signals.CHECK_INTERVAL + 0.01
    redis.exists.side_effect = None
    redis.exists.return_value = 1
    assert await signals.is_set(message_id)
