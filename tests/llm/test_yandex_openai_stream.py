"""Потоковая выдача OpenAI-совместимого API (ТЗ §6, ответ по мере генерации).

Сеть подменена httpx.MockTransport: поток «data: {json}» с приращениями,
расход в последнем куске, откат на обычный вызов, если поток не принят.
"""

import asyncio
import json
from typing import Any

import httpx
import pytest

from corp_ed.llm.errors import LLMError
from corp_ed.llm.types import Completion, FinishReason, Message, Role
from corp_ed.llm.yandex_openai import YandexOpenAIAdapter

MESSAGES = [
    Message(role=Role.SYSTEM, content="Ты ассистент."),
    Message(role=Role.USER, content="Сколько дней отпуска?"),
]


def _chunk(content: str | None = None, reason: str | None = None) -> dict[str, Any]:
    delta: dict[str, Any] = {} if content is None else {"content": content}
    return {
        "model": "aliceai-llm-flash/rc",
        "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
    }


def _sse(*chunks: dict[str, Any], done: bool = True) -> bytes:
    lines = [f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


USAGE = {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 7}}


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


async def _collect(adapter: YandexOpenAIAdapter) -> tuple[list[str], Completion]:
    pieces: list[str] = []
    completion: Completion | None = None
    async for item in adapter.stream(MESSAGES, temperature=0.1, max_tokens=500):
        if isinstance(item, Completion):
            completion = item
        else:
            pieces.append(item)
    assert completion is not None
    return pieces, completion


async def test_stream_yields_deltas_then_completion_with_usage() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=_sse(
                _chunk("Отпуск "),
                _chunk("— 28 дней"),
                _chunk(" [1].", reason="stop"),
                USAGE,
            ),
            headers={"content-type": "text/event-stream"},
        )

    pieces, completion = await _collect(_adapter(handler))

    assert pieces == ["Отпуск ", "— 28 дней", " [1]."]
    assert completion.content == "Отпуск — 28 дней [1]."
    assert completion.finish_reason is FinishReason.COMPLETED
    assert (completion.usage.input_tokens, completion.usage.output_tokens) == (120, 7)
    assert completion.model == "aliceai-llm-flash"
    assert completion.model_version == "aliceai-llm-flash/rc"
    body = seen[0]
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["model"] == "gpt://b1gfolder/aliceai-llm-flash/latest"
    assert (body["temperature"], body["max_tokens"]) == (0.1, 500)


async def test_cumulative_chunks_are_turned_into_deltas() -> None:
    """Провайдер шлёт текст с начала (как нативный API Yandex) — наружу
    идёт только новое."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse(
                _chunk("Отпуск"),
                _chunk("Отпуск — 28"),
                _chunk("Отпуск — 28 дней.", reason="stop"),
            ),
        )

    pieces, completion = await _collect(_adapter(handler))

    assert pieces == ["Отпуск", " — 28", " дней."]
    assert completion.content == "Отпуск — 28 дней."


async def test_missing_usage_is_estimated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_sse(_chunk("Двадцать восемь дней.", "stop"))
        )

    _, completion = await _collect(_adapter(handler))

    assert completion.usage.input_tokens > 0
    assert completion.usage.output_tokens > 0


async def test_content_filter_and_length_are_mapped() -> None:
    def filtered(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(_chunk("Я не", "content_filter")))

    def truncated(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(_chunk("Длинный", "length")))

    assert (await _collect(_adapter(filtered)))[
        1
    ].finish_reason is FinishReason.FILTERED
    assert (await _collect(_adapter(truncated)))[
        1
    ].finish_reason is FinishReason.TRUNCATED


async def test_rejected_stream_falls_back_to_plain_call() -> None:
    """400 на потоковый запрос (например, провайдер не знает stream_options)
    — тот же вызов без потока, ответ одним куском."""
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if body.get("stream"):
            return httpx.Response(400, json={"error": {"message": "unknown field"}})
        return httpx.Response(
            200,
            json={
                "model": "aliceai-llm-flash/latest",
                "choices": [
                    {"message": {"content": "28 дней [1]."}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3},
            },
        )

    pieces, completion = await _collect(_adapter(handler))

    assert pieces == ["28 дней [1]."]
    assert completion.usage.output_tokens == 3
    assert [bool(c.get("stream")) for c in calls] == [True, False]
    assert "stream_options" not in calls[1]


async def test_retryable_error_before_first_byte_is_retried() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, json={"error": {"message": "quota"}})
        return httpx.Response(200, content=_sse(_chunk("Да.", "stop")))

    pieces, _ = await _collect(_adapter(handler))

    assert pieces == ["Да."]
    assert attempts == 2


async def test_auth_error_is_not_masked_by_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    with pytest.raises(LLMError):
        await _collect(_adapter(handler))


async def test_malformed_chunk_and_cut_stream_are_errors() -> None:
    def garbage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"data: {not json}\n\n")

    def cut(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(_chunk("Отпуск"), done=False))

    with pytest.raises(LLMError):
        await _collect(_adapter(garbage))
    with pytest.raises(LLMError):
        await _collect(_adapter(cut))


async def test_stream_holds_concurrency_slot_until_closed() -> None:
    """Слот квоты занят, пока идёт поток; закрытый на середине поток
    слот отдаёт."""
    semaphore = asyncio.Semaphore(1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(_chunk("Раз "), _chunk("два.", "stop")))

    adapter = _adapter(handler, concurrency=semaphore)
    stream = adapter.stream(MESSAGES)
    first = await anext(stream)
    assert first == "Раз "
    assert semaphore.locked()
    await stream.aclose()
    assert not semaphore.locked()
