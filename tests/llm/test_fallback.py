"""Запасная модель ответа и предохранитель (BH-50).

Поставщики — подделки: основная отвечает, молчит, падает быстрой ошибкой
или ошибкой настройки; часы предохранителя — свои. Сроки и повторы
адаптера — в test_fallback_adapter.py.
"""

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from corp_ed.core.metrics import (
    FALLBACK_REASONS,
    FALLBACK_STAGES,
    LLM_BREAKER_STATE,
    LLM_FALLBACK,
)
from corp_ed.llm.errors import ErrorKind, LLMError
from corp_ed.llm.fallback import BREAKER_OPEN, CircuitBreaker, FallbackGateway
from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.throttle import ThrottleBusyError
from corp_ed.llm.types import Completion, FinishReason, Message, Role, Usage

MESSAGES = [Message(role=Role.USER, content="Сколько дней отпуска?")]


def _completion(content: str, provider: str, finish: FinishReason) -> Completion:
    return Completion(
        content=content,
        finish_reason=finish,
        usage=Usage(input_tokens=10, output_tokens=5),
        model_version=provider,
        model=provider,
        latency_ms=0,
        provider=provider,
    )


class Provider(LLMGateway):
    """Поставщик-подделка: по очереди ответы или ошибки.

    Сценарий — список: str — ответ (кусками по словам в потоке),
    LLMError — отказ до первого куска; ("broken", text) — первый кусок и
    обрыв."""

    def __init__(self, name: str, *script: Any, filtered: bool = False) -> None:
        self.name = name
        self.script = list(script)
        self.filtered = filtered
        self.calls = 0

    def _next(self) -> Any:
        self.calls += 1
        step = self.script.pop(0) if self.script else "ok"
        if isinstance(step, LLMError):
            raise step
        return step

    def _done(self, content: str) -> Completion:
        finish = FinishReason.FILTERED if self.filtered else FinishReason.COMPLETED
        return _completion(content, self.name, finish)

    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
        response_format: dict[str, Any] | None = None,
    ) -> Completion:
        return self._done(self._next())

    async def stream(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
    ) -> AsyncGenerator[str | Completion, None]:
        step = self._next()
        if isinstance(step, tuple):
            yield step[1]
            raise LLMError("stream broken", retryable=False, kind="stream_broken")
        for word in step.split(" "):
            yield word
        yield self._done(step)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _error(kind: ErrorKind) -> LLMError:
    return LLMError(kind, retryable=kind in ("timeout", "error_fast"), kind=kind)


def _gateway(
    primary: Provider, reserve: Provider | None = None, clock: Clock | None = None
) -> FallbackGateway:
    breaker = CircuitBreaker(failures=3, open_seconds=120, clock=clock or Clock())
    return FallbackGateway(primary, reserve or Provider("cloudru"), breaker)


async def _stream(gateway: LLMGateway) -> tuple[list[str], Completion]:
    pieces: list[str] = []
    completion: Completion | None = None
    async for item in gateway.stream(MESSAGES):
        if isinstance(item, Completion):
            completion = item
        else:
            pieces.append(item)
    assert completion is not None
    return pieces, completion


async def test_primary_answers_reserve_is_not_called() -> None:
    reserve = Provider("cloudru")
    gateway = _gateway(Provider("yandex", "Двадцать восемь дней"), reserve)

    pieces, completion = await _stream(gateway)

    assert "".join(pieces) == "Двадцатьвосемьдней"
    assert (completion.provider, completion.fallback_reason) == ("yandex", None)
    assert reserve.calls == 0


@pytest.mark.parametrize("kind", ["timeout", "error_fast", "error_config"])
async def test_failure_before_first_chunk_goes_to_reserve(kind: ErrorKind) -> None:
    reserve = Provider("cloudru", "Ответ резерва")
    gateway = _gateway(Provider("yandex", _error(kind)), reserve)

    pieces, completion = await _stream(gateway)

    assert pieces == ["Ответ", "резерва"]
    assert completion.provider == "cloudru"
    assert completion.fallback_reason == kind
    assert reserve.calls == 1


async def test_generate_goes_to_reserve_too() -> None:
    gateway = _gateway(Provider("yandex", _error("timeout")), Provider("cloudru"))

    completion = await gateway.generate(MESSAGES)

    assert (completion.provider, completion.fallback_reason) == ("cloudru", "timeout")


async def test_config_error_opens_the_breaker_at_once() -> None:
    clock = Clock()
    primary = Provider("yandex", _error("error_config"))
    gateway = _gateway(primary, clock=clock)

    await _stream(gateway)

    assert gateway.primary_down()
    assert LLM_BREAKER_STATE.labels("yandex", "chat")._value.get() == 2
    _, completion = await _stream(gateway)
    assert completion.fallback_reason == BREAKER_OPEN
    assert primary.calls == 1


async def test_three_failures_open_the_breaker_then_probe_closes_it() -> None:
    clock = Clock()
    primary = Provider("yandex", *[_error("timeout")] * 3, "Снова Flash")
    reserve = Provider("cloudru")
    gateway = _gateway(primary, reserve, clock)

    for _ in range(2):
        await _stream(gateway)
        assert not gateway.primary_down()
    await _stream(gateway)
    assert gateway.primary_down()

    # Открыт: Flash не зовём и срок не ждём — сразу резерв.
    _, completion = await _stream(gateway)
    assert completion.fallback_reason == BREAKER_OPEN
    assert primary.calls == 3

    # Через 2 минуты — проба: Flash ответил, предохранитель закрыт.
    clock.now += 120
    _, completion = await _stream(gateway)
    assert (completion.provider, completion.fallback_reason) == ("yandex", None)
    assert not gateway.primary_down()
    assert LLM_BREAKER_STATE.labels("yandex", "chat")._value.get() == 0
    assert reserve.calls == 4


async def test_failed_probe_keeps_it_open_for_another_period() -> None:
    clock = Clock()
    primary = Provider("yandex", *[_error("error_fast")] * 4)
    gateway = _gateway(primary, clock=clock)
    for _ in range(3):
        await _stream(gateway)

    clock.now += 120
    _, completion = await _stream(gateway)

    assert completion.fallback_reason == "error_fast"
    assert gateway.primary_down()
    clock.now += 119
    assert gateway.primary_down()


async def test_success_resets_the_streak() -> None:
    primary = Provider(
        "yandex", _error("timeout"), _error("timeout"), "ok", _error("timeout")
    )
    gateway = _gateway(primary)

    for _ in range(4):
        await _stream(gateway)

    assert not gateway.primary_down()


async def test_break_after_first_chunk_stays_an_error() -> None:
    # Решение команды (п. 3): начатый ответ на экране не подменяем.
    reserve = Provider("cloudru")
    gateway = _gateway(Provider("yandex", ("broken", "Отпуск ")), reserve)

    shown: list[str] = []
    with pytest.raises(LLMError) as raised:
        async for item in gateway.stream(MESSAGES):
            if isinstance(item, str):
                shown.append(item)

    assert raised.value.kind == "stream_broken"
    assert shown == ["Отпуск "]
    assert reserve.calls == 0


async def test_content_filter_is_not_a_provider_failure() -> None:
    reserve = Provider("cloudru")
    gateway = _gateway(Provider("yandex", "Не могу", filtered=True), reserve)

    _, completion = await _stream(gateway)

    assert completion.finish_reason is FinishReason.FILTERED
    assert completion.fallback_reason is None
    assert reserve.calls == 0


async def test_our_own_queue_is_not_counted() -> None:
    # ThrottleBusyError — перегрузка у нас (п. 5): остаётся busy, резерв не
    # зовём, предохранитель не трогаем.
    reserve = Provider("cloudru")
    primary = Provider("yandex", *[ThrottleBusyError(5.0)] * 3)
    gateway = _gateway(primary, reserve)

    for _ in range(3):
        with pytest.raises(ThrottleBusyError):
            await _stream(gateway)

    assert reserve.calls == 0
    assert not gateway.primary_down()


async def test_both_fail_is_an_error() -> None:
    gateway = _gateway(
        Provider("yandex", _error("timeout")), Provider("cloudru", _error("timeout"))
    )

    with pytest.raises(LLMError):
        await _stream(gateway)


async def test_without_reserve_only_the_breaker() -> None:
    # Ночные подписи пробелов: отказ — ошибка; открыт — сразу ошибка.
    primary = Provider("yandex", *[_error("timeout")] * 3)
    gateway = FallbackGateway(primary, None, CircuitBreaker(clock=Clock()))

    for _ in range(3):
        with pytest.raises(LLMError):
            await gateway.generate(MESSAGES)
    assert gateway.primary_down()
    with pytest.raises(LLMError):
        await gateway.generate(MESSAGES)
    assert primary.calls == 3


def test_fallback_metric_has_every_label_pair_from_the_start() -> None:
    samples = {
        (s.labels["stage"], s.labels["reason"])
        for metric in LLM_FALLBACK.collect()
        for s in metric.samples
        if s.name.endswith("_total")
    }
    assert samples == {(st, r) for st in FALLBACK_STAGES for r in FALLBACK_REASONS}
