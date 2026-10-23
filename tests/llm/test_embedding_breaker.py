"""Эмбеддинги при отказе поставщика (BH-51).

Вопрос: короткий срок 5 с, таймаут не повторяется, 1 повтор на быструю
ошибку; предохранитель открыт — эмбеддинг не вызывается, сразу ошибка.
"""

from typing import Any

import httpx
import pytest

from corp_ed.core.config import LLMSettings
from corp_ed.llm.embedding_breaker import BreakerEmbeddings
from corp_ed.llm.embedding_gateway import EmbeddingGateway
from corp_ed.llm.errors import LLMError
from corp_ed.llm.factory import build_embedding_gateway
from corp_ed.llm.fallback import CircuitBreaker
from corp_ed.llm.throttle import ThrottleBusyError
from corp_ed.llm.types import EmbeddingResult
from corp_ed.llm.yandex_embedding import YandexEmbeddingAdapter

VECTOR = {"embedding": [0.1] * 256, "modelVersion": "v", "numTokens": 3}


def _adapter(handler: Any) -> YandexEmbeddingAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return YandexEmbeddingAdapter(
        client=client,
        folder_id="b1g",
        api_key="key",
        family="text-search",
        dim=256,
        base_delay=1.0,
        query_timeout=5.0,
        query_attempts=2,
        max_delay=0.01,
    )


async def test_query_timeout_is_short_and_not_retried() -> None:
    seen: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"]["read"])
        raise httpx.ReadTimeout("silent", request=request)

    with pytest.raises(LLMError) as raised:
        await _adapter(handler).embed_query("отпуск")

    assert raised.value.kind == "timeout"
    assert seen == [5.0]


async def test_query_fast_error_is_retried_once() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={})

    with pytest.raises(LLMError):
        await _adapter(handler).embed_query("отпуск")

    assert calls == 2


async def test_documents_keep_the_long_deadline() -> None:
    seen: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json=VECTOR)

    await _adapter(handler).embed_document("текст")

    assert seen == [60.0]


class Script(EmbeddingGateway):
    def __init__(self, *errors: LLMError | None) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def _next(self) -> EmbeddingResult:
        self.calls += 1
        error = self.errors.pop(0) if self.errors else None
        if error is not None:
            raise error
        return EmbeddingResult(
            embedding=[0.0], model="m", model_version="v", latency_ms=0, input_tokens=1
        )

    async def embed_document(self, text: str) -> EmbeddingResult:
        return await self._next()

    async def embed_query(self, text: str) -> EmbeddingResult:
        return await self._next()


def _timeout() -> LLMError:
    return LLMError("t", retryable=True, kind="timeout")


async def test_three_failures_open_it_and_the_provider_is_not_called() -> None:
    inner = Script(_timeout(), _timeout(), _timeout())
    embeddings = BreakerEmbeddings(inner, CircuitBreaker(kind="embedding"))

    for _ in range(3):
        with pytest.raises(LLMError):
            await embeddings.embed_query("q")
    assert embeddings.down()
    with pytest.raises(LLMError):
        await embeddings.embed_query("q")

    assert inner.calls == 3


async def test_rejected_text_and_our_queue_do_not_count() -> None:
    rejected = LLMError("400", retryable=False, status=400)
    inner = Script(rejected, rejected, ThrottleBusyError(5.0), ThrottleBusyError(5.0))
    embeddings = BreakerEmbeddings(inner, CircuitBreaker(failures=2, kind="embedding"))

    for _ in range(4):
        with pytest.raises(LLMError):
            await embeddings.embed_document("q")

    assert not embeddings.down()


async def test_payment_error_opens_it_at_once() -> None:
    inner = Script(LLMError("402", retryable=False, kind="error_payment", status=402))
    embeddings = BreakerEmbeddings(inner, CircuitBreaker(kind="embedding"))

    with pytest.raises(LLMError):
        await embeddings.embed_query("q")

    assert embeddings.down()


async def test_factory_wraps_yandex_embeddings() -> None:
    settings = LLMSettings(  # type: ignore[call-arg]
        llm_provider="yandex-openai", yc_folder_id="f", yc_api_key="k"
    )
    async with httpx.AsyncClient() as client:
        gateway = build_embedding_gateway(client, settings)

    assert isinstance(gateway, BreakerEmbeddings)
    assert isinstance(gateway.inner, YandexEmbeddingAdapter)
