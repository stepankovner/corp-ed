"""Адаптер OpenAI-совместимого API (Alice AI LLM Flash, BH-15).

Сеть подменена httpx.MockTransport: проверяется то, что уходит к
провайдеру, и то, как разбирается ответ, в том числе кривой.
"""

import asyncio
import json
from typing import Any

import httpx
import pytest

from corp_ed.llm.errors import LLMError
from corp_ed.llm.types import FinishReason, Message, Role
from corp_ed.llm.yandex import YandexAdapter
from corp_ed.llm.yandex_openai import YandexOpenAIAdapter, parse_chat_response

MESSAGES = [
    Message(role=Role.SYSTEM, content="Ты ассистент."),
    Message(role=Role.USER, content="Сколько дней отпуска?"),
]


def _chat_body(content: str = "28 дней [1].", reason: str = "stop") -> dict[str, Any]:
    return {
        "model": "aliceai-llm-flash/latest",
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": reason,
            }
        ],
        "usage": {"prompt_tokens": 1787, "completion_tokens": 55},
    }


def _adapter(
    handler: Any, *, concurrency: asyncio.Semaphore | None = None
) -> YandexOpenAIAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return YandexOpenAIAdapter(
        client=client,
        folder_id="b1gfolder",
        api_key="secret-key",
        model="aliceai-llm-flash",
        concurrency=concurrency,
        base_delay=0,
    )


async def test_request_shape_and_parsing() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_chat_body())

    completion = await _adapter(handler).generate(MESSAGES, temperature=0)

    request = seen[0]
    payload = json.loads(request.content)
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["Authorization"] == "Api-Key secret-key"
    assert request.headers["OpenAI-Project"] == "b1gfolder"
    assert payload["model"] == "gpt://b1gfolder/aliceai-llm-flash/latest"
    assert payload["temperature"] == 0
    assert payload["messages"][1] == {
        "role": "user",
        "content": "Сколько дней отпуска?",
    }

    assert completion.content == "28 дней [1]."
    assert completion.finish_reason is FinishReason.COMPLETED
    assert completion.usage.input_tokens == 1787
    assert completion.usage.output_tokens == 55
    assert completion.model == "aliceai-llm-flash"


async def test_length_is_truncated() -> None:
    completion = await _adapter(
        lambda r: httpx.Response(200, json=_chat_body(reason="length"))
    ).generate(MESSAGES)
    assert completion.finish_reason is FinishReason.TRUNCATED


async def test_retryable_status_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_: float) -> None:
        pass

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    responses = [httpx.Response(429, json={}), httpx.Response(200, json=_chat_body())]

    completion = await _adapter(lambda r: responses.pop(0)).generate(MESSAGES)

    assert completion.content == "28 дней [1]."


async def test_client_error_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": {"message": "bad"}})

    with pytest.raises(LLMError) as info:
        await _adapter(handler).generate(MESSAGES)
    assert info.value.retryable is False
    assert calls == 1


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"choices": []},
        {"choices": "oops"},
        {"choices": [{"message": None}]},
        [],
        None,
    ],
)
def test_malformed_body_is_llm_error(body: Any) -> None:
    """RISKS №4: чужие KeyError/IndexError не должны вылетать наружу."""
    with pytest.raises(LLMError) as info:
        parse_chat_response(body, model="m", latency_ms=1)
    assert info.value.retryable is False


def test_content_filter_is_llm_error() -> None:
    with pytest.raises(LLMError):
        parse_chat_response(
            _chat_body(reason="content_filter"), model="m", latency_ms=1
        )


async def test_non_json_success_body_is_llm_error() -> None:
    with pytest.raises(LLMError):
        await _adapter(lambda r: httpx.Response(200, text="<html>")).generate(MESSAGES)


async def test_api_key_is_not_in_error_message() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "unauthorized"}})

    with pytest.raises(LLMError) as info:
        await _adapter(handler).generate(MESSAGES)
    assert "secret-key" not in str(info.value)


async def test_concurrency_is_limited() -> None:
    """Квота — 10 одновременных запросов на каталог: семафор держит лимит."""
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return httpx.Response(200, json=_chat_body())

    adapter = _adapter(handler, concurrency=asyncio.Semaphore(2))
    await asyncio.gather(*(adapter.generate(MESSAGES) for _ in range(6)))

    assert peak == 2


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"result": {"alternatives": []}},
        {"result": {"alternatives": [{"status": "ALTERNATIVE_STATUS_FINAL"}]}},
        {"result": "oops"},
    ],
)
async def test_native_adapter_malformed_body_is_llm_error(body: Any) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    )
    adapter = YandexAdapter(client=client, folder_id="f", api_key="k")

    with pytest.raises(LLMError) as info:
        await adapter.generate(MESSAGES)
    assert info.value.retryable is False
