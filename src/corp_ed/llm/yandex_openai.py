"""Адаптер OpenAI-совместимого API Yandex AI Studio (BH-15).

Через /v1/chat/completions доступны все модели каталога, кроме
нативного YandexGPT-only API: в том числе Alice AI LLM Flash, выбранная
в досье (9.2) условно по замерам 24–25.09 — 0,20 ₽ против 0,37 ₽ у Lite
за ответ, p95 2,0 с против 4,1 с, F1 отказа 0,98 против 0,94.

Свой адаптер на httpx, а не SDK openai: тот же клиент, те же ретраи и
та же классификация ошибок, что у нативного адаптера, и ни одной новой
зависимости.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from corp_ed.llm.errors import LLMError
from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.retry import call_with_retry
from corp_ed.llm.types import Completion, FinishReason, Message, Usage

URL = "https://llm.api.cloud.yandex.net/v1/chat/completions"

_FINISH_REASONS = {
    "stop": FinishReason.COMPLETED,
    "length": FinishReason.TRUNCATED,
}


def model_uri(folder_id: str, model: str) -> str:
    """«aliceai-llm-flash» → gpt://<каталог>/aliceai-llm-flash/latest.

    Версию можно указать явно: «yandexgpt/rc».
    """
    return f"gpt://{folder_id}/{model if '/' in model else model + '/latest'}"


def parse_chat_response(body: Any, model: str, latency_ms: int) -> Completion:
    """Ответ /v1/chat/completions → Completion.

    Тело — данные извне без гарантий формы. Любая неожиданность (нет
    choices, не тот тип, неизвестный finish_reason) — LLMError, а не
    KeyError: чужие исключения прошли бы мимо обработчиков и дали 500
    без внятного лога (RISKS №4).
    """
    try:
        choice = body["choices"][0]
        content = choice["message"].get("content") or ""
        raw_reason = choice.get("finish_reason")
        usage = body.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens", 0))
        output_tokens = int(usage.get("completion_tokens", 0))
        model_version = str(body.get("model") or model)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise LLMError(
            f"unexpected chat response shape: {str(body)[:200]}", retryable=False
        ) from exc

    if raw_reason not in _FINISH_REASONS:
        # content_filter и прочее: ответа нет, повтор даст то же самое.
        raise LLMError(f"unsupported finish reason: {raw_reason}", retryable=False)

    return Completion(
        content=str(content).strip(),
        finish_reason=_FINISH_REASONS[raw_reason],
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        model_version=model_version,
        model=model,
        latency_ms=latency_ms,
    )


class YandexOpenAIAdapter(LLMGateway):
    def __init__(
        self,
        client: httpx.AsyncClient,
        folder_id: str,
        api_key: str,
        model: str = "aliceai-llm-flash",
        *,
        concurrency: asyncio.Semaphore | None = None,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        read_timeout: float = 60.0,
    ):
        self._client = client
        self._folder_id = folder_id
        self._api_key = api_key
        self._model = model
        # Квота генерации — 10 ОДНОВРЕМЕННЫХ запросов на каталог (замер
        # ML 25.09, 429 сверх неё). Семафор общий на процесс: адаптер
        # создаётся на запрос, а лимит — на всё приложение.
        self._concurrency = concurrency
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._read_timeout = read_timeout

    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
        response_format: dict[str, Any] | None = None,
    ) -> Completion:
        payload: dict[str, Any] = {
            "model": model_uri(self._folder_id, self._model),
            "messages": [
                {"role": m.role.value, "content": m.content} for m in messages
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        timings: list[int] = []

        async def do_request() -> httpx.Response:
            started = time.perf_counter()
            response = await self._client.post(
                URL,
                json=payload,
                headers={
                    "Authorization": f"Api-Key {self._api_key}",
                    "OpenAI-Project": self._folder_id,
                },
                timeout=httpx.Timeout(
                    connect=5.0, read=self._read_timeout, write=10.0, pool=5.0
                ),
            )
            timings.append(int((time.perf_counter() - started) * 1000))
            return response

        if self._concurrency is None:
            response = await self._call(do_request)
        else:
            async with self._concurrency:
                response = await self._call(do_request)

        try:
            body = response.json()
        except ValueError as exc:
            raise LLMError("chat response is not JSON", retryable=False) from exc
        return parse_chat_response(body, model=self._model, latency_ms=timings[-1])

    async def _call(
        self, do_request: Callable[[], Awaitable[httpx.Response]]
    ) -> httpx.Response:
        return await call_with_retry(
            do_request,
            max_attempts=self._max_attempts,
            base_delay=self._base_delay,
        )
