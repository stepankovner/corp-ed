import asyncio
import contextlib
import re
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.types import Completion, FinishReason, Message, Usage


class FakeAdapter(LLMGateway):
    model_name = "fake"

    def __init__(
        self,
        content: str = "фейковый ответ",
        finish_reason: FinishReason = FinishReason.COMPLETED,
    ):
        self.content = content
        # Потоковая выдача в тестах: на какие куски резать ответ и чего
        # ждать перед каждым (остановка посреди ответа).
        self.pieces: list[str] | None = None
        self.before_piece: Callable[[int], Awaitable[None]] | None = None
        self.calls: list[list[Message]] = []
        self.call_kwargs: list[dict[str, float | int]] = []
        self.response_formats: list[dict[str, Any] | None] = []
        self.finish_reason = finish_reason

    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
        response_format: dict[str, Any] | None = None,
    ) -> Completion:
        self.calls.append(messages)
        self.call_kwargs.append({"temperature": temperature, "max_tokens": max_tokens})
        self.response_formats.append(response_format)

        return Completion(
            content=self.content,
            finish_reason=self.finish_reason,
            usage=Usage(
                input_tokens=0,
                output_tokens=0,
            ),
            model_version="fake",
            model="fake",
            latency_ms=0,
        )

    async def stream(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
    ) -> AsyncGenerator[str | Completion, None]:
        completion = await self.generate(
            messages, temperature=temperature, max_tokens=max_tokens
        )
        pieces = self.pieces if self.pieces is not None else [completion.content]
        for index, piece in enumerate(pieces):
            if self.before_piece is not None:
                await self.before_piece(index)
            if piece:
                yield piece
        yield completion


_FIRST_EXCERPT = re.compile(
    r"^\[1\][^\n]*\n(.+?)(?:\n\n\[2\]|\n\nВопрос сотрудника:)", re.S | re.M
)


_CONDENSE_QUESTION = re.compile(r"\nНовый вопрос: (.+)\nОтвет:\Z", re.S)


class DevAdapter(LLMGateway):
    """Модель для разработки (LLM_PROVIDER=fake): без сети и ключей.

    Ответ по документам — начало первой найденной выдержки со ссылкой [1],
    чтобы фронт показывал настоящие источники; общий ответ — короткая
    заглушка. Токены считаются по длине текста — кредиты списываются как
    в бою. В production запрещена настройками.

    Поток — по словам с паузой stream_delay: интерфейс печатает ответ
    так же, как с настоящей моделью, и его можно остановить.

    Для нагрузочной проверки (docs/LOAD-TEST.md): latency — пауза до первого
    слова (и вся длительность ответа без потока), concurrency — семафор
    генерации процесса, слот занят на всё время ответа, как у настоящей
    модели.
    """

    model_name = "dev"

    def __init__(
        self,
        stream_delay: float = 0.03,
        *,
        latency: float = 0.0,
        concurrency: asyncio.Semaphore | None = None,
    ) -> None:
        self.stream_delay = stream_delay
        self.latency = latency
        self._concurrency = concurrency

    async def stream(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
    ) -> AsyncGenerator[str | Completion, None]:
        async with self._slot():
            await self._wait()
            completion = self._complete(messages)
            for word in re.findall(r"\S+\s*", completion.content):
                await asyncio.sleep(self.stream_delay)
                yield word
            yield completion

    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
        response_format: dict[str, Any] | None = None,
    ) -> Completion:
        async with self._slot():
            await self._wait()
            return self._complete(messages)

    def _slot(self) -> contextlib.AbstractAsyncContextManager[Any]:
        if self._concurrency is None:
            return contextlib.nullcontext()
        return self._concurrency

    async def _wait(self) -> None:
        if self.latency:
            await asyncio.sleep(self.latency)

    def _complete(self, messages: list[Message]) -> Completion:
        prompt = messages[-1].content if messages else ""
        condense = _CONDENSE_QUESTION.search(prompt)
        match = _FIRST_EXCERPT.search(prompt)
        if condense:
            # Переписывание уточняющего вопроса (BH-28): без модели вопрос
            # остаётся как есть — поиск идёт по нему.
            content = condense.group(1).strip()
        elif match:
            fragment = " ".join(match.group(1).split())[:280]
            content = (
                f"Режим разработки, ответ без модели. По документам: {fragment} [1]"
            )
        else:
            content = "Режим разработки: общий ответ без модели."
        tokens_in = sum(len(m.content) for m in messages) // 3
        return Completion(
            content=content,
            finish_reason=FinishReason.COMPLETED,
            usage=Usage(input_tokens=tokens_in, output_tokens=len(content) // 3),
            model_version="dev",
            model="dev",
            latency_ms=0,
        )
