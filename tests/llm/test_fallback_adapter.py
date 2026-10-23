"""Короткие сроки адаптера и запасной поставщик Cloud.ru (BH-50).

Сеть подменена httpx.MockTransport. Сроки фабрики: 1 повтор только на
быструю ошибку с паузой не больше max_delay, таймаут — без повтора;
класс отказа (LLMError.kind) — по нему переход в резерв.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from corp_ed.core.config import LLMSettings
from corp_ed.llm.errors import LLMError
from corp_ed.llm.factory import build_llm_gateway
from corp_ed.llm.fallback import FallbackGateway
from corp_ed.llm.types import Completion, Message, Role
from corp_ed.llm.yandex_openai import CloudRuAdapter, YandexOpenAIAdapter

MESSAGES = [Message(role=Role.USER, content="Сколько дней отпуска?")]

FAST = {
    "max_attempts": 2,
    "base_delay": 1.0,
    "max_delay": 0.01,
    "retry_on_timeout": False,
    "read_timeout": 8.0,
    "call_timeout": 8.0,
    "stream_timeout": 30.0,
}


def _chunk(content: str, reason: str | None = None) -> str:
    body = {
        "model": "m",
        "choices": [
            {"index": 0, "delta": {"content": content}, "finish_reason": reason}
        ],
    }
    return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"


def _answer(content: str) -> dict[str, Any]:
    return {
        "model": "openai/gpt-oss-120b",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3},
    }


def _yandex(handler: Any, **options: Any) -> YandexOpenAIAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return YandexOpenAIAdapter(
        client=client,
        folder_id="b1gfolder",
        api_key="secret-key",
        model="aliceai-llm-flash",
        **{**FAST, **options},
    )


async def _drain(adapter: YandexOpenAIAdapter) -> tuple[list[str], Completion]:
    pieces: list[str] = []
    completion: Completion | None = None
    async for item in adapter.stream(MESSAGES, temperature=0, max_tokens=100):
        if isinstance(item, Completion):
            completion = item
        else:
            pieces.append(item)
    assert completion is not None
    return pieces, completion


async def test_timeout_before_first_chunk_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("silent", request=request)

    with pytest.raises(LLMError) as raised:
        await _drain(_yandex(handler))

    assert raised.value.kind == "timeout"
    assert calls == 1


async def test_fast_error_is_retried_once_with_a_short_pause() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": "busy"})

    started = time.perf_counter()
    with pytest.raises(LLMError) as raised:
        await _drain(_yandex(handler))

    assert raised.value.kind == "error_fast"
    assert calls == 2
    # Пауза — не больше max_delay, а не 1 с + джиттер.
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize(
    ("status", "kind"),
    [(401, "error_config"), (403, "error_config"), (402, "error_payment")],
)
async def test_config_and_payment_errors_are_not_retried(
    status: int, kind: str
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"error": "no"})

    with pytest.raises(LLMError) as raised:
        await _drain(_yandex(handler))

    assert raised.value.kind == kind
    assert calls == 1


async def test_break_after_first_chunk_is_stream_broken() -> None:
    async def body() -> AsyncIterator[bytes]:
        yield _chunk("Отпуск ").encode()
        raise httpx.ReadTimeout("gone")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body())

    shown: list[str] = []
    with pytest.raises(LLMError) as raised:
        async for item in _yandex(handler).stream(MESSAGES):
            if isinstance(item, str):
                shown.append(item)

    assert shown == ["Отпуск "]
    assert raised.value.kind == "stream_broken"


async def test_whole_stream_deadline() -> None:
    async def body() -> AsyncIterator[bytes]:
        while True:
            yield _chunk("слово ").encode()
            await asyncio.sleep(0.02)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body())

    with pytest.raises(LLMError) as raised:
        await _drain(_yandex(handler, stream_timeout=0.05))

    assert raised.value.kind == "stream_broken"


async def test_generate_has_a_deadline_for_the_whole_call() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, json=_answer("поздно"))

    with pytest.raises(LLMError) as raised:
        await _yandex(handler, call_timeout=0.05).generate(MESSAGES)

    assert raised.value.kind == "timeout"


async def test_cloudru_request_and_provider() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_answer("Двадцать восемь дней."))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = CloudRuAdapter(
        client, "https://foundation-models.api.cloud.ru/v1/", "cloud-key", **FAST
    )

    completion = await adapter.generate(MESSAGES, temperature=0, max_tokens=1000)

    [request] = seen
    assert (
        str(request.url) == "https://foundation-models.api.cloud.ru/v1/chat/completions"
    )
    assert request.headers["authorization"] == "Bearer cloud-key"
    assert "x-data-logging-enabled" not in request.headers
    payload = json.loads(request.content)
    assert payload["model"] == "openai/gpt-oss-120b"
    assert payload["reasoning_effort"] == "low"
    assert (payload["temperature"], payload["max_tokens"]) == (0, 1000)
    assert completion.provider == "cloudru"
    assert completion.content == "Двадцать восемь дней."


def _settings(**values: Any) -> LLMSettings:
    return LLMSettings(  # type: ignore[call-arg]
        llm_provider="yandex-openai", yc_folder_id="f", yc_api_key="k", **values
    )


async def test_factory_wraps_flash_only_with_a_cloudru_key() -> None:
    async with httpx.AsyncClient() as client:
        plain = build_llm_gateway(client, _settings())
        wrapped = build_llm_gateway(
            client,
            _settings(cloudru_base_url="https://cloud.example/v1", cloudru_api_key="c"),
        )
        nightly = build_llm_gateway(client, _settings(), reserve=False)

    assert isinstance(plain, YandexOpenAIAdapter)
    assert not plain.primary_down()
    assert isinstance(wrapped, FallbackGateway)
    assert isinstance(wrapped.reserve, CloudRuAdapter)
    assert isinstance(nightly, FallbackGateway)
    assert nightly.reserve is None
