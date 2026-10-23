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
import contextlib
import json
import random
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import replace
from typing import Any

import httpx
import structlog

from corp_ed.core.metrics import LLM_FIRST_CHUNK
from corp_ed.domain.tokens import count_tokens
from corp_ed.llm.errors import LLMError, classify
from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.observe import current_stage, outcome_of, record_call
from corp_ed.llm.retry import call_with_retry
from corp_ed.llm.types import Completion, FinishReason, Message, Usage
from corp_ed.llm.yandex_headers import auth_headers

logger = structlog.get_logger()

URL = "https://llm.api.cloud.yandex.net/v1/chat/completions"

_STREAM_REJECTED = frozenset({400, 404, 405, 415, 422})
"""Ответы, после которых поток не повторяем, а зовём модель без него."""

_FINISH_REASONS = {
    "stop": FinishReason.COMPLETED,
    "length": FinishReason.TRUNCATED,
    "content_filter": FinishReason.FILTERED,
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
        # Неизвестная причина: ответа нет, повтор даст то же самое.
        raise LLMError(f"unsupported finish reason: {raw_reason}", retryable=False)

    return Completion(
        content=str(content).strip(),
        finish_reason=_FINISH_REASONS[raw_reason],
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
        model_version=model_version,
        model=model,
        latency_ms=latency_ms,
    )


class _StreamState:
    """Разбор потока /v1/chat/completions (строки «data: {json}»).

    Как и у обычного ответа, тело — данные извне: неожиданная форма —
    LLMError, а не KeyError. Куски — приращения (delta) по контракту
    OpenAI; если провайдер шлёт весь текст с начала (так делает нативный
    API Yandex), это видно со второго куска, и наружу идёт только новое.
    """

    def __init__(self) -> None:
        self.text = ""
        self.done = False
        self._chunks = 0
        self._cumulative = False
        self._finish: str | None = None
        self._usage: dict[str, Any] | None = None
        self._model_version: str | None = None

    def feed(self, line: str) -> str:
        if not line.startswith("data:"):
            return ""
        data = line[len("data:") :].strip()
        if data == "[DONE]":
            self.done = True
            return ""
        try:
            chunk = json.loads(data)
            if not isinstance(chunk, dict):
                raise TypeError("chunk is not an object")
            if isinstance(chunk.get("usage"), dict):
                self._usage = chunk["usage"]
            if chunk.get("model"):
                self._model_version = str(chunk["model"])
            choices = chunk.get("choices") or []
            if not choices:
                return ""
            choice = choices[0]
            delta = choice.get("delta") or {}
            content = str(delta.get("content") or "")
            if choice.get("finish_reason"):
                self._finish = str(choice["finish_reason"])
        except (ValueError, TypeError, AttributeError, IndexError) as exc:
            raise LLMError(
                f"unexpected stream chunk: {data[:200]}", retryable=False
            ) from exc
        if not content:
            return ""
        self._chunks += 1
        if self._chunks == 2 and len(content) > len(self.text):
            self._cumulative = content.startswith(self.text)
        piece = content[len(self.text) :] if self._cumulative else content
        self.text += piece
        return piece

    def completion(
        self, messages: list[Message], *, model: str, latency_ms: int
    ) -> Completion:
        if self._finish is None and not self.done:
            raise LLMError("stream ended without finish reason", retryable=False)
        reason = self._finish or "stop"
        if reason not in _FINISH_REASONS:
            raise LLMError(f"unsupported finish reason: {reason}", retryable=False)
        if self._usage is not None:
            try:
                input_tokens = int(self._usage.get("prompt_tokens", 0))
                output_tokens = int(self._usage.get("completion_tokens", 0))
            except (TypeError, ValueError) as exc:
                raise LLMError("unexpected stream usage", retryable=False) from exc
        else:
            # Провайдер не прислал расход (stream_options не поддержан):
            # кредиты — по оценке, как у остановленного ответа.
            logger.warning("llm_stream_usage_estimated")
            input_tokens = sum(count_tokens(m.content) for m in messages)
            output_tokens = count_tokens(self.text)
        return Completion(
            content=self.text.strip(),
            finish_reason=_FINISH_REASONS[reason],
            usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
            model_version=self._model_version or model,
            model=model,
            latency_ms=latency_ms,
        )


def _outcome(completion: Completion) -> str:
    return "filtered" if completion.finish_reason is FinishReason.FILTERED else "ok"


class YandexOpenAIAdapter(LLMGateway):
    provider = "yandex"

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
        retry_on_timeout: bool = True,
        max_delay: float | None = None,
        call_timeout: float | None = None,
        stream_timeout: float | None = None,
    ):
        """Сроки (BH-50): read_timeout — до первого куска и между кусками;
        call_timeout — весь вызов без потока вместе с повторами;
        stream_timeout — весь поток. retry_on_timeout=False — таймаут не
        повторять, max_delay — потолок паузы между повторами. По умолчанию
        — прежние 60 с × 3 попытки; короткие сроки задаёт фабрика."""
        self._client = client
        self._folder_id = folder_id
        self._api_key = api_key
        self._model = model
        self.model_name = model
        # Квота генерации — 10 ОДНОВРЕМЕННЫХ запросов на каталог (замер
        # ML 25.09, 429 сверх неё). Семафор общий на процесс: адаптер
        # создаётся на запрос, а лимит — на всё приложение.
        self._concurrency = concurrency
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._read_timeout = read_timeout
        self._retry_on_timeout = retry_on_timeout
        self._max_delay = max_delay
        self._call_timeout = call_timeout
        self._stream_timeout = stream_timeout

    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
        response_format: dict[str, Any] | None = None,
    ) -> Completion:
        payload = self._payload(messages, temperature, max_tokens)
        if response_format is not None:
            payload["response_format"] = response_format
        stage = current_stage()
        started = time.perf_counter()
        try:
            if self._concurrency is None:
                completion = await self._complete(payload)
            else:
                async with self._concurrency:
                    completion = await self._complete(payload)
        except LLMError as exc:
            self._record(stage, outcome_of(exc), started)
            raise
        self._record(stage, _outcome(completion), started)
        return completion

    def _record(self, stage: str | None, outcome: str, started: float) -> None:
        record_call(
            self.provider, "chat", stage, outcome, time.perf_counter() - started
        )

    async def stream(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
    ) -> AsyncGenerator[str | Completion, None]:
        """Потоковая выдача /v1/chat/completions (stream=true, ТЗ §6).

        Слот квоты (семафор) занят, пока идёт поток. Повторы — только до
        первого байта: начатый ответ сотрудник уже видит. Провайдер не
        принял потоковый запрос (400, 404, 422 — например, не знает
        stream_options) — тот же вызов без потока, одним куском.
        """
        payload = self._payload(messages, temperature, max_tokens)
        stage = current_stage()
        started = time.perf_counter()
        first = True
        try:
            async with contextlib.AsyncExitStack() as stack:
                if self._concurrency is not None:
                    await stack.enter_async_context(self._concurrency)
                async for item in self._stream(payload, messages):
                    if first and stage is not None:
                        first = False
                        LLM_FIRST_CHUNK.labels(self.provider).observe(
                            time.perf_counter() - started
                        )
                    if isinstance(item, Completion):
                        self._record(stage, _outcome(item), started)
                    yield item
        except LLMError as exc:
            if first and stage is not None and exc.kind == "timeout":
                LLM_FIRST_CHUNK.labels(self.provider).observe(self._read_timeout)
            self._record(stage, outcome_of(exc), started)
            raise

    def _url(self) -> str:
        return URL

    def _model_ref(self) -> str:
        return model_uri(self._folder_id, self._model)

    def _payload(
        self, messages: list[Message], temperature: float, max_tokens: int
    ) -> dict[str, Any]:
        return {
            "model": self._model_ref(),
            "messages": [
                {"role": m.role.value, "content": m.content} for m in messages
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

    def _headers(self) -> dict[str, str]:
        return {**auth_headers(self._api_key), "OpenAI-Project": self._folder_id}

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(connect=5.0, read=self._read_timeout, write=10.0, pool=5.0)

    async def _complete(self, payload: dict[str, Any]) -> Completion:
        timings: list[int] = []

        async def do_request() -> httpx.Response:
            started = time.perf_counter()
            response = await self._client.post(
                self._url(),
                json=payload,
                headers=self._headers(),
                timeout=self._timeout(),
            )
            timings.append(int((time.perf_counter() - started) * 1000))
            return response

        if self._call_timeout is None:
            response = await self._call(do_request)
        else:
            try:
                async with asyncio.timeout(self._call_timeout):
                    response = await self._call(do_request)
            except TimeoutError as exc:
                raise LLMError(
                    f"no answer in {self._call_timeout} s",
                    retryable=True,
                    kind="timeout",
                ) from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise LLMError(
                "chat response is not JSON", retryable=False, kind="error_fast"
            ) from exc
        completion = parse_chat_response(
            body, model=self._model, latency_ms=timings[-1]
        )
        return replace(completion, provider=self.provider)

    async def _stream(
        self, payload: dict[str, Any], messages: list[Message]
    ) -> AsyncGenerator[str | Completion, None]:
        started = time.perf_counter()
        response = await self._open_stream(
            {**payload, "stream": True, "stream_options": {"include_usage": True}}
        )
        if response is None:
            completion = await self._complete(payload)
            if completion.content:
                yield completion.content
            yield completion
            return

        parsed = _StreamState()
        shown = False
        try:
            async for line in response.aiter_lines():
                piece = parsed.feed(line)
                if piece:
                    shown = True
                    yield piece
                if parsed.done:
                    break
                if (
                    self._stream_timeout is not None
                    and time.perf_counter() - started > self._stream_timeout
                ):
                    raise LLMError(
                        f"stream longer than {self._stream_timeout} s",
                        retryable=False,
                        kind="stream_broken" if shown else "timeout",
                    )
        except httpx.TimeoutException as exc:
            # До первого куска — молчание поставщика; после — обрыв.
            raise LLMError(
                f"stream timeout: {exc}",
                retryable=False,
                kind="stream_broken" if shown else "timeout",
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(
                f"stream broken: {exc}",
                retryable=False,
                kind="stream_broken" if shown else "error_fast",
            ) from exc
        finally:
            await response.aclose()
        completion = parsed.completion(
            messages,
            model=self._model,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        yield replace(completion, provider=self.provider)

    async def _open_stream(self, payload: dict[str, Any]) -> httpx.Response | None:
        """Открытый поток со статусом 200; None — провайдер не принял поток."""
        error: LLMError | None = None
        for attempt in range(self._max_attempts):
            request = self._client.build_request(
                "POST",
                self._url(),
                json=payload,
                headers=self._headers(),
                timeout=self._timeout(),
            )
            try:
                response = await self._client.send(request, stream=True)
            except httpx.TimeoutException as exc:
                error = LLMError(f"timeout: {exc}", retryable=True, kind="timeout")
                if not self._retry_on_timeout:
                    raise error from exc
            except httpx.TransportError as exc:
                error = LLMError(f"transport: {exc}", retryable=True)
            else:
                if response.status_code == 200:
                    return response
                await response.aread()
                await response.aclose()
                if response.status_code in _STREAM_REJECTED:
                    logger.warning("llm_stream_fallback", status=response.status_code)
                    return None
                error = classify(response)
            if error is None or not error.retryable:
                break
            if attempt < self._max_attempts - 1:
                # Джиттер паузы между повторами, не криптография.
                delay = self._base_delay * (2**attempt) + random.uniform(0, 1)  # noqa: S311
                if self._max_delay is not None:
                    delay = min(delay, self._max_delay)
                logger.warning("llm_retry", attempt=attempt + 1, error=str(error))
                await asyncio.sleep(delay)
        if error is None:
            raise RuntimeError("unreachable: stream opened without status")
        logger.error("llm_call_failed", attempts=self._max_attempts, error=str(error))
        raise error

    async def _call(
        self, do_request: Callable[[], Awaitable[httpx.Response]]
    ) -> httpx.Response:
        return await call_with_retry(
            do_request,
            max_attempts=self._max_attempts,
            base_delay=self._base_delay,
            retry_on_timeout=self._retry_on_timeout,
            max_delay=self._max_delay,
        )


class CloudRuAdapter(YandexOpenAIAdapter):
    """Запасная модель ответа (BH-50): Cloud.ru Foundation Models,
    OpenAI-совместимый /chat/completions.

    Тот же разбор ответа и потока, те же сроки, что у Flash. Отличия:
    адрес и ключ (Bearer сервисного аккаунта), имя модели уходит как есть
    («openai/gpt-oss-120b»), reasoning_effort=low. Размышления модели,
    если придут отдельным полем, сотруднику не показываются: в поток идёт
    только delta.content. Заголовка Яндекса об отказе от логирования здесь
    нет — у Cloud.ru запросы своих моделей не логируются (ml-providers.md).
    """

    provider = "cloudru"

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        api_key: str,
        model: str = "openai/gpt-oss-120b",
        **options: Any,
    ):
        super().__init__(client, folder_id="", api_key=api_key, model=model, **options)
        self._base_url = base_url.rstrip("/")

    def _url(self) -> str:
        return f"{self._base_url}/chat/completions"

    def _model_ref(self) -> str:
        return self._model

    def _payload(
        self, messages: list[Message], temperature: float, max_tokens: int
    ) -> dict[str, Any]:
        return {
            **super()._payload(messages, temperature, max_tokens),
            "reasoning_effort": "low",
        }

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}
