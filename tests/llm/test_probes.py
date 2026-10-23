"""Пробные вызовы и метрики главного цикла (BH-52).

Проба раз в 5 минут, при незакрытом предохранителе — раз в 2; успех
закрывает предохранитель, неудачи считаются подряд. Вызовы сотрудников —
в corp_ed_llm_calls_total по исходу, пробы — только в своём счётчике.
Метки всех corp_ed_llm_* — только из перечней.
"""

import contextlib
import json
from typing import Any

import httpx
from prometheus_client import REGISTRY

from corp_ed.core.metrics import (
    FALLBACK_REASONS,
    FALLBACK_STAGES,
    LLM_BREAKER_STATE,
    LLM_CALL_STAGES,
    LLM_CALLS,
    LLM_KINDS,
    LLM_OUTCOMES,
    LLM_PROBE,
    LLM_PROBE_FAILURES,
    LLM_PROVIDERS,
)
from corp_ed.llm.errors import LLMError
from corp_ed.llm.fallback import CircuitBreaker
from corp_ed.llm.observe import llm_stage
from corp_ed.llm.probes import EVERY, WHILE_OPEN, Prober
from corp_ed.llm.types import Message, Role
from corp_ed.llm.yandex_openai import YandexOpenAIAdapter
from tests.llm.test_embedding_breaker import Script
from tests.llm.test_fallback import Clock, Provider

MESSAGES = [Message(role=Role.USER, content="Сколько дней отпуска?")]
SECRET_QUESTION = "Сколько получает Петров Иван Сергеевич?"


def _state(kind: str = "chat") -> float:
    return LLM_BREAKER_STATE.labels("yandex", kind)._value.get()


def _probes(kind: str, outcome: str, provider: str = "yandex") -> float:
    return LLM_PROBE.labels(provider, kind, outcome)._value.get()


def _calls(stage: str, outcome: str) -> float:
    return LLM_CALLS.labels("yandex", "chat", stage, outcome)._value.get()


def _timeout() -> LLMError:
    return LLMError("t", retryable=True, kind="timeout")


async def test_three_timeouts_open_it_then_a_probe_closes_it() -> None:
    clock = Clock()
    breaker = CircuitBreaker(clock=clock)
    for _ in range(3):
        breaker.record_failure("timeout")
    assert _state() == 2

    flash = Provider("yandex", "да")
    prober = Prober(chat=flash, chat_breaker=breaker, clock=clock)
    before = _probes("chat", "ok")

    # Срок не вышел: проба всё равно идёт — она и есть выход из «открыт».
    await prober.run_once()

    assert flash.calls == 1
    assert breaker.state() == "closed"
    assert _state() == 0
    assert _probes("chat", "ok") == before + 1


async def test_failed_probes_are_counted_in_a_row() -> None:
    clock = Clock()
    embeddings = Script(_timeout(), _timeout(), None)
    breaker = CircuitBreaker(kind="embedding", clock=clock)
    prober = Prober(embeddings=embeddings, embedding_breaker=breaker, clock=clock)

    await prober.run_once()
    clock.now += EVERY
    await prober.run_once()

    assert LLM_PROBE_FAILURES.labels("yandex", "embedding")._value.get() == 2

    clock.now += EVERY
    await prober.run_once()
    assert LLM_PROBE_FAILURES.labels("yandex", "embedding")._value.get() == 0


async def test_probe_every_five_minutes_and_every_two_while_not_closed() -> None:
    clock = Clock()
    breaker = CircuitBreaker(failures=1, clock=clock)
    flash = Provider("yandex", "да", _timeout(), "да")
    prober = Prober(chat=flash, chat_breaker=breaker, clock=clock)

    await prober.run_once()
    clock.now += EVERY - 1
    await prober.run_once()
    assert flash.calls == 1

    clock.now += 1
    await prober.run_once()
    assert flash.calls == 2

    # Отказ открыл предохранитель — следующая проба через 2 минуты.
    clock.now += WHILE_OPEN
    await prober.run_once()
    assert flash.calls == 3


async def test_reserve_probe_has_its_own_rate_and_no_breaker() -> None:
    clock = Clock()
    reserve = Provider("cloudru", "да")
    prober = Prober(reserve=reserve, clock=clock)
    before = _probes("chat", "ok", "cloudru")

    await prober.run_once()
    clock.now += EVERY
    await prober.run_once()

    assert reserve.calls == 1
    assert _probes("chat", "ok", "cloudru") == before + 1


def _flash(handler: Any) -> YandexOpenAIAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return YandexOpenAIAdapter(
        client=client,
        folder_id="b1g",
        api_key="key",
        model="aliceai-llm-flash",
        max_attempts=2,
        base_delay=0,
        max_delay=0,
        retry_on_timeout=False,
    )


def _answer(content: str, reason: str = "stop") -> dict[str, Any]:
    return {
        "model": "m",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": reason,
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1},
    }


async def test_calls_are_counted_by_stage_and_outcome() -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={})

    def filtered(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_answer("", "content_filter"))

    fast = _calls("condense", "error_fast")
    blocked = _calls("gaps_label", "filtered")

    with llm_stage("condense"), contextlib.suppress(LLMError):
        await _flash(failing).generate(MESSAGES)
    with llm_stage("gaps_label"):
        await _flash(filtered).generate(MESSAGES)

    # Два запроса с повтором — один вызов.
    assert _calls("condense", "error_fast") == fast + 1
    assert _calls("gaps_label", "filtered") == blocked + 1


async def test_probes_are_not_counted_as_calls() -> None:
    def ok(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["max_tokens"] == 5
        return httpx.Response(200, json=_answer("да"))

    before = sum(_calls(stage, "ok") for stage in FALLBACK_STAGES)

    await Prober(chat=_flash(ok)).run_once()

    assert sum(_calls(stage, "ok") for stage in FALLBACK_STAGES) == before


def test_llm_metric_labels_come_only_from_the_lists() -> None:
    allowed = {
        "provider": set(LLM_PROVIDERS),
        "kind": set(LLM_KINDS),
        "stage": set(FALLBACK_STAGES)
        | {s for v in LLM_CALL_STAGES.values() for s in v},
        "outcome": set(LLM_OUTCOMES),
        "reason": set(FALLBACK_REASONS),
    }
    seen = 0
    for family in REGISTRY.collect():
        if not family.name.startswith("corp_ed_llm_"):
            continue
        for sample in family.samples:
            for label, value in sample.labels.items():
                if label == "le":
                    continue
                assert value in allowed[label], (family.name, label, value)
                assert SECRET_QUESTION not in value
                seen += 1
    assert seen > 0
