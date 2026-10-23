"""Дублирующий запрос шага решения в eval (BH-46 «Бюджет времени шага решения
и дублирующий запрос», п. 2–3 и 8; фаза 2) — на подделках с настоящими
короткими паузами: задержка дубля 0,1 с вместо 2 с."""

import json
import threading
import time
from typing import Any

import pytest

from corp_ed.domain.decision import Route, parse_decision
from corp_ed.llm.types import Message, Role
from eval.decision_loop import run_decided
from eval.providers import (
    CallTimeoutError,
    ChainExhaustedError,
    HedgedDecider,
    ProviderError,
)
from eval.yandex import Completion, YandexError

MESSAGES = [Message(role=Role.USER, content="вопрос")]
DECISION = json.dumps(
    {
        "kind": "question",
        "subject": "—",
        "excerpts_subject": "—",
        "match": "n/a",
        "sufficiency": "full",
        "missing": "—",
        "search_query": "—",
    }
)


class Leg:
    """Нога дубля: пауза spend секунд, потом заготовка (исключение — бросает)."""

    def __init__(self, outcome: Any, spend: float, model: str) -> None:
        self.outcome, self.spend, self.model = outcome, spend, model
        self.calls: list[dict[str, Any]] = []
        self.finished = threading.Event()

    def complete(self, messages: Any, **kwargs: Any) -> Completion:
        self.calls.append(kwargs)
        time.sleep(self.spend)
        self.finished.set()
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return Completion(
            text=self.outcome,
            input_tokens=100,
            output_tokens=20,
            latency_ms=self.spend * 1000,
            model=self.model,
        )


def hedged(primary: Leg, hedge: Leg, **kw: Any) -> HedgedDecider:
    return HedgedDecider(
        primary,
        "openai/gpt-oss-120b",
        hedge,
        "qwen3.6-35b-a3b",
        delay=kw.pop("delay", 0.1),
        valid=lambda text: parse_decision(text) is not None,
        **kw,
    )


def cloudru(outcome: Any, spend: float) -> Leg:
    return Leg(outcome, spend, "openai/gpt-oss-120b")


def qwen(outcome: Any, spend: float) -> Leg:
    return Leg(outcome, spend, "qwen3.6-35b-a3b")


def call(d: HedgedDecider, **kw: Any) -> Completion:
    return d.complete(
        MESSAGES,
        model="не используется",
        api="openai",
        temperature=0,
        extra={"reasoning_effort": "low"},
        **kw,
    )


def test_primary_in_time_no_hedge() -> None:
    primary, hedge = cloudru(DECISION, 0.02), qwen(DECISION, 0.01)
    d = hedged(primary, hedge)
    completion = call(d, timeout=1.0)
    assert completion.model == "openai/gpt-oss-120b"
    assert hedge.calls == []
    assert primary.calls[0]["timeout"] == 1.0
    assert primary.calls[0]["extra"] == {"reasoning_effort": "low"}
    assert primary.calls[0]["model"] == "openai/gpt-oss-120b"
    [event] = d.drain()
    assert not event.hedged and event.winner == "primary" and event.effort == "low"
    assert event.wasted == {} and not event.abandoned


def test_silent_primary_loses_to_hedge_after_delay() -> None:
    primary, hedge = cloudru(DECISION, 0.4), qwen(DECISION, 0.02)
    d = hedged(primary, hedge)
    started = time.perf_counter()
    completion = call(d, timeout=1.0)
    wall = time.perf_counter() - started
    assert completion.model == "qwen3.6-35b-a3b"
    # Время ответа — от начала вызова, с ожиданием дубля (≈ 0,1 + 0,02 с).
    assert 100 <= completion.latency_ms <= 300 and wall < 0.35
    # Дублёру — свои поля (без размышлений) и без таймаута (у клиента
    # Яндекса его нет); схема и прочее — те же.
    assert hedge.calls[0]["extra"] == {"reasoning_effort": "none"}
    assert "timeout" not in hedge.calls[0]
    assert hedge.calls[0]["api"] == "openai" and hedge.calls[0]["temperature"] == 0
    [event] = d.drain()  # ждёт проигравшего основного
    assert event.hedged and event.trigger == "delay" and event.winner == "hedge"
    assert 90 <= event.launch_ms <= 250
    assert event.wasted == {"openai/gpt-oss-120b": [100, 20]}
    assert not event.abandoned


@pytest.mark.parametrize(
    "error",
    [ProviderError("cloudru", 502, "Bad Gateway"), ProviderError("cloudru", 429, "")],
)
def test_fast_error_sends_hedge_at_once(error: ProviderError) -> None:
    primary, hedge = cloudru(error, 0.0), qwen(DECISION, 0.02)
    d = hedged(primary, hedge, delay=0.5)
    completion = call(d, timeout=1.0)
    assert completion.model == "qwen3.6-35b-a3b" and completion.latency_ms < 200
    [event] = d.drain()
    assert event.trigger == "error" and event.launch_ms < 100
    assert event.primary_status == error.status and not event.alert
    assert event.wasted == {}  # упавший основной токенов не тратил


def test_config_error_alerts() -> None:
    primary, hedge = (
        cloudru(ProviderError("cloudru", 402, "нет денег"), 0.0),
        qwen(DECISION, 0.0),
    )
    d = hedged(primary, hedge)
    assert call(d).model == "qwen3.6-35b-a3b"
    [event] = d.drain()
    assert event.alert and event.primary_outcome == "config"


def test_invalid_primary_sends_hedge_and_wastes_its_tokens() -> None:
    primary, hedge = cloudru("{не по схеме", 0.0), qwen(DECISION, 0.02)
    d = hedged(primary, hedge, delay=0.5)
    assert call(d).model == "qwen3.6-35b-a3b"
    [event] = d.drain()
    assert event.trigger == "invalid" and event.primary_outcome == "invalid"
    assert event.wasted == {"openai/gpt-oss-120b": [100, 20]}


def test_both_invalid_returns_primary_answer() -> None:
    primary, hedge = cloudru("{", 0.0), qwen("{", 0.0)
    d = hedged(primary, hedge)
    completion = call(d)
    assert completion.model == "openai/gpt-oss-120b" and completion.text == "{"
    [event] = d.drain()
    assert event.winner == "" and event.wasted == {"qwen3.6-35b-a3b": [100, 20]}


def test_hedge_error_keeps_waiting_for_primary() -> None:
    primary = cloudru(DECISION, 0.25)
    hedge = qwen(YandexError(500, "Internal"), 0.0)
    d = hedged(primary, hedge)
    completion = call(d, timeout=1.0)
    assert completion.model == "openai/gpt-oss-120b"
    assert 230 <= completion.latency_ms <= 450
    [event] = d.drain()
    assert event.winner == "primary" and event.hedge_outcome == "server"


def test_both_silent_time_out_by_budget() -> None:
    primary, hedge = cloudru(DECISION, 0.6), qwen(DECISION, 0.6)
    d = hedged(primary, hedge, drain_wait=0.01)
    started = time.perf_counter()
    with pytest.raises(CallTimeoutError) as caught:
        call(d, timeout=0.3)
    assert 0.28 <= time.perf_counter() - started < 0.45
    assert 280 <= caught.value.elapsed_ms < 450
    [event] = d.drain()
    assert event.hedged and event.winner == "" and event.abandoned


def test_both_down_raise_exhausted() -> None:
    primary = cloudru(ProviderError("cloudru", 503, ""), 0.0)
    hedge = qwen(YandexError(503, ""), 0.0)
    with pytest.raises(ChainExhaustedError):
        call(hedged(primary, hedge), timeout=1.0)


def test_hedge_winner_drives_decision_loop_with_delay_in_wall_time() -> None:
    # Цикл решения с дублем: быстрый вызов (low) — основной молчит, решение
    # дублёра; время по стене — с задержкой дубля.
    primary, hedge = cloudru(DECISION, 0.4), qwen(DECISION, 0.02)
    d = hedged(primary, hedge)

    def decide(messages: list[Message]) -> Completion:
        return d.complete(
            messages, model="", timeout=1.0, extra={"reasoning_effort": "low"}
        )

    run = run_decided(
        "Сколько дней отпуска?",
        [type("Chunk", (), {"content": "28 дней", "title": "Д", "heading_path": []})()],
        decide=decide,
        answer=lambda messages: Completion(
            text="28 дней [1].",
            input_tokens=1,
            output_tokens=1,
            latency_ms=1000.0,
            model="flash",
        ),
        search=lambda q: [],
    )
    assert run.route is Route.ANSWER and run.text == "28 дней [1]."
    assert run.decision_calls[0].model == "qwen3.6-35b-a3b"
    assert 100 <= run.wall_ms - 1000.0 <= 300
    assert d.drain()[0].winner == "hedge"


def test_hedge_timeout_falls_back_in_loop() -> None:
    primary, hedge = cloudru(DECISION, 0.5), qwen(DECISION, 0.5)
    d = hedged(primary, hedge, drain_wait=0.01)

    def decide(messages: list[Message]) -> Completion:
        return d.complete(messages, model="", timeout=0.2)

    run = run_decided(
        "Есть ли ипотека?",
        [],
        decide=decide,
        answer=lambda messages: Completion("", 0, 0, 0.0, "flash"),
        search=lambda q: [],
    )
    assert run.timeout_reason == "fast" and run.needs_general
    assert run.steps[0][0] == "decide!timeout"
