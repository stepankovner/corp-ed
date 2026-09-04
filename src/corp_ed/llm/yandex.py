import time
from typing import Any

import httpx

from corp_ed.llm.errors import LLMError
from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.retry import call_with_retry
from corp_ed.llm.types import Completion, FinishReason, Message, Usage

URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

_FINISH_REASONS = {
    "ALTERNATIVE_STATUS_FINAL": FinishReason.COMPLETED,
    "ALTERNATIVE_STATUS_TRUNCATED_FINAL": FinishReason.TRUNCATED,
}


class YandexAdapter(LLMGateway):
    def __init__(
        self,
        client: httpx.AsyncClient,
        folder_id: str,
        api_key: str,
        model: str = "yandexgpt-lite",
        max_attempts: int = 3,
        base_delay: float = 1.0,
        read_timeout: float = 60.0,
    ):
        self._folder_id = folder_id
        self._client = client
        self._api_key = api_key
        self._model = model
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._read_timeout = read_timeout

    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
    ) -> Completion:
        payload = {
            "modelUri": f"gpt://{self._folder_id}/{self._model}",
            "completionOptions": {
                "stream": False,
                "temperature": temperature,
                "maxTokens": max_tokens,
            },
            "messages": [{"role": m.role.value, "text": m.content} for m in messages],
        }

        timings: list[int] = []

        async def do_request() -> httpx.Response:
            started = time.perf_counter()
            response = await self._client.post(
                URL,
                json=payload,
                headers={"Authorization": f"Api-Key {self._api_key}"},
                timeout=httpx.Timeout(
                    connect=5.0,
                    read=self._read_timeout,
                    write=10.0,
                    pool=5.0,
                ),
            )
            timings.append(int((time.perf_counter() - started) * 1000))
            return response

        response = await call_with_retry(
            do_request,
            max_attempts=self._max_attempts,
            base_delay=self._base_delay,
        )

        return self._parse(response.json(), latency_ms=timings[-1])

    def _parse(self, body: dict[str, Any], latency_ms: int) -> Completion:
        result = body["result"]
        alternative = result["alternatives"][0]
        usage = result["usage"]

        raw_status = alternative["status"]
        if raw_status not in _FINISH_REASONS:
            raise LLMError(f"unknown finish status: {raw_status}", retryable=False)

        return Completion(
            content=alternative["message"]["text"],
            finish_reason=_FINISH_REASONS[raw_status],
            usage=Usage(
                input_tokens=int(usage["inputTextTokens"]),
                output_tokens=int(usage["completionTokens"]),
            ),
            model_version=result["modelVersion"],
            model=self._model,
            latency_ms=latency_ms,
        )
