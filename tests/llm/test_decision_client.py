"""Поставщики шага решения и дублирующий запрос (BH-46 «Поставщики» п. 8).

Звенья — подделки со своим временем ответа; задержки и бюджет сжаты в 100
раз (3 с → 0,05 с), чтобы тест шёл миллисекунды.
"""

import asyncio
import json
from typing import Any

import httpx
import pytest

from corp_ed.core.metrics import DECISION_CALLS, DECISION_FAILOVER
from corp_ed.llm.decision_client import (
    HedgedDecider,
    cloudru_leg,
    yandex_leg,
)
from corp_ed.llm.errors import LLMError
from corp_ed.llm.types import Completion, FinishReason, Message, Role, Usage

MESSAGES = [Message(role=Role.USER, content="Кто согласует закупку?")]
GOOD = '{"kind": "question"}'
SCHEMA: dict[str, Any] = {"type": "json_schema"}


def _valid(text: str) -> bool:
    return text == GOOD


class Leg:
    def __init__(
        self,
        name: str,
        *,
        after: float,
        text: str = GOOD,
        error: LLMError | None = None,
    ) -> None:
        self.name = name
        self.model = f"{name}-model"
        self.after = after
        self.text = text
        self.error = error
        self.calls: list[str] = []
        self.cancelled = False

    async def complete(
        self,
        messages: list[Message],
        *,
        effort: str,
        response_format: dict[str, Any],
        timeout: float,
    ) -> Completion:
        self.calls.append(effort)
        try:
            await asyncio.sleep(self.after)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.error is not None:
            raise self.error
        return Completion(
            content=self.text,
            finish_reason=FinishReason.COMPLETED,
            usage=Usage(input_tokens=100, output_tokens=20),
            model_version="v",
            model=self.model,
            latency_ms=int(self.after * 1000),
        )


def _decider(primary: Leg, hedge: Leg) -> HedgedDecider:
    return HedgedDecider(
        primary, hedge, delays={"low": 0.05, "medium": None}, valid=_valid
    )


async def _decide(decider: HedgedDecider, effort: str = "low", budget: float = 1.0):
    return await decider.decide(
        MESSAGES, effort=effort, call="first", response_format=SCHEMA, budget=budget
    )


async def test_fast_primary_no_hedge() -> None:
    cloud, qwen = Leg("cloudru", after=0.001), Leg("yandex", after=0.001)

    result = await _decide(_decider(cloud, qwen))

    assert result.completion is not None
    assert (result.winner, result.provider, result.hedged) == (
        "primary",
        "cloudru",
        False,
    )
    assert qwen.calls == []


async def test_silent_primary_hedge_wins_and_primary_is_cancelled() -> None:
    cloud, qwen = Leg("cloudru", after=5), Leg("yandex", after=0.005)
    before = DECISION_FAILOVER.labels("hedge")._value.get()

    result = await _decide(_decider(cloud, qwen))

    assert (result.winner, result.provider, result.trigger) == (
        "hedge",
        "yandex",
        "delay",
    )
    assert cloud.cancelled
    # Ответ дублёра — примерно через задержку и его время, не через бюджет.
    assert result.ms < 500
    assert [leg.outcome for leg in result.legs] == ["cancelled", "ok"]
    assert DECISION_FAILOVER.labels("hedge")._value.get() == before + 1


async def test_no_hedge_on_medium_recheck() -> None:
    cloud, qwen = Leg("cloudru", after=0.2), Leg("yandex", after=0.001)

    result = await _decide(_decider(cloud, qwen), effort="medium")

    assert result.winner == "primary"
    assert qwen.calls == []


async def test_fast_error_goes_to_hedge_at_once() -> None:
    error = LLMError("503", retryable=True, status=503)
    cloud, qwen = Leg("cloudru", after=0.001, error=error), Leg("yandex", after=0.001)

    result = await _decide(_decider(cloud, qwen))

    assert (result.winner, result.trigger) == ("hedge", "error_fast")
    assert result.ms < 500


async def test_payment_error_hedge_answers_and_the_call_is_counted() -> None:
    error = LLMError("402", retryable=False, kind="error_payment", status=402)
    cloud, qwen = Leg("cloudru", after=0.001, error=error), Leg("yandex", after=0.001)
    before = DECISION_CALLS.labels(
        "cloudru", "low", "first", "error_payment"
    )._value.get()

    result = await _decide(_decider(cloud, qwen))

    assert result.winner == "hedge"
    assert result.legs[0].status == 402
    after = DECISION_CALLS.labels(
        "cloudru", "low", "first", "error_payment"
    )._value.get()
    assert after == before + 1


async def test_invalid_primary_hedge_at_once() -> None:
    cloud, qwen = Leg("cloudru", after=0.001, text="{"), Leg("yandex", after=0.001)

    result = await _decide(_decider(cloud, qwen))

    assert (result.winner, result.trigger) == ("hedge", "invalid")


async def test_both_invalid_returns_the_primary_answer() -> None:
    cloud = Leg("cloudru", after=0.001, text="{")
    qwen = Leg("yandex", after=0.001, text="[")

    result = await _decide(_decider(cloud, qwen))

    assert result.completion is not None and result.completion.content == "{"
    assert result.winner == "primary"


async def test_both_silent_budget_runs_out() -> None:
    cloud, qwen = Leg("cloudru", after=5), Leg("yandex", after=5)

    result = await _decide(_decider(cloud, qwen), budget=0.1)

    assert result.completion is None
    assert result.timeout
    assert cloud.cancelled and qwen.cancelled
    assert result.ms < 1000


async def test_both_fail_is_unavailable() -> None:
    error = LLMError("500", retryable=True, status=500)
    cloud = Leg("cloudru", after=0.001, error=error)
    qwen = Leg("yandex", after=0.001, error=error)

    result = await _decide(_decider(cloud, qwen))

    assert result.completion is None
    assert not result.timeout


async def test_no_budget_left_no_call() -> None:
    cloud, qwen = Leg("cloudru", after=0.001), Leg("yandex", after=0.001)

    result = await _decide(_decider(cloud, qwen), budget=0)

    assert result.timeout and cloud.calls == [] and qwen.calls == []


async def test_legs_send_the_strict_schema_and_their_effort() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": GOOD},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cloud = cloudru_leg(
            client, "https://cloud.example/v1/", "ck", "openai/gpt-oss-120b"
        )
        qwen = yandex_leg(client, "b1g", "yk", "qwen3.6-35b-a3b")
        for leg in (cloud, qwen):
            await leg.complete(
                MESSAGES, effort="low", response_format=SCHEMA, timeout=1
            )

    first, second = (json.loads(r.content) for r in seen)
    assert str(seen[0].url) == "https://cloud.example/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer ck"
    assert (first["model"], first["reasoning_effort"]) == ("openai/gpt-oss-120b", "low")
    assert first["response_format"] == SCHEMA and first["max_tokens"] == 2000
    assert second["model"] == "gpt://b1g/qwen3.6-35b-a3b/latest"
    assert second["reasoning_effort"] == "none"
    assert seen[1].headers["x-data-logging-enabled"] == "false"


async def test_leg_timeout_is_an_llm_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        leg = cloudru_leg(client, "https://c/v1", "k", "m")
        with pytest.raises(LLMError) as raised:
            await leg.complete(
                MESSAGES, effort="low", response_format=SCHEMA, timeout=1
            )

    assert raised.value.kind == "timeout"


async def test_factory_needs_the_cloudru_key() -> None:
    from corp_ed.core.config import LLMSettings, RagSettings
    from corp_ed.llm.factory import build_decider

    rag = RagSettings(  # type: ignore[call-arg]
        context_max_tokens=3000,
        faq_limit=5,
        faq_max_distance=0.59,
        faq_temperature=0,
        retriever="vector",
        chunk_tokens=400,
        overlap_tokens=50,
        fulltext_weight=0.5,
    )
    base = {"llm_provider": "yandex-openai", "yc_folder_id": "f", "yc_api_key": "k"}
    async with httpx.AsyncClient() as client:
        none = build_decider(client, LLMSettings(**base), rag)  # type: ignore[arg-type]
        decider = build_decider(
            client,
            LLMSettings(  # type: ignore[arg-type]
                **base, cloudru_base_url="https://c/v1", cloudru_api_key="c"
            ),
            rag,
        )

    assert none is None
    assert decider is not None
    assert (decider.primary.name, decider.hedge.name) == ("cloudru", "yandex")  # type: ignore[union-attr]
    assert decider.delays == {"low": 3.0, "medium": None}


def test_decision_metric_labels_come_only_from_the_lists() -> None:
    from prometheus_client import REGISTRY

    from corp_ed.core import metrics as m

    allowed = {
        "mode": set(m.DECISION_MODES),
        "route": set(m.DECISION_ROUTES),
        "provider": set(m.DECISION_PROVIDERS),
        "effort": set(m.DECISION_EFFORTS),
        "call": set(m.DECISION_CALL_KINDS),
        "stage": set(m.DECISION_STAGES),
        "direction": {"input", "output"},
        "kind": set(m.DECISION_BRANCHES),
    }
    by_name = {
        "corp_ed_decision_fallback": {"reason": set(m.DECISION_FALLBACK_REASONS)},
        "corp_ed_decision_failover": {"reason": set(m.DECISION_FAILOVER_REASONS)},
        "corp_ed_decision_calls": {"outcome": set(m.DECISION_OUTCOMES)},
        "corp_ed_decision_speculative": {
            "outcome": set(m.DECISION_SPECULATIVE),
            "reason": {r for rs in m.DECISION_SPECULATIVE.values() for r in rs},
        },
        "corp_ed_decision_branch": {
            "result": {r for rs in m.DECISION_BRANCHES.values() for r in rs}
        },
    }
    seen = 0
    for family in REGISTRY.collect():
        if not family.name.startswith(("corp_ed_decision_", "corp_ed_faq_decision")):
            continue
        for sample in family.samples:
            for label, value in sample.labels.items():
                if label == "le":
                    continue
                values = by_name.get(family.name, {}).get(label) or allowed[label]
                assert value in values, (family.name, label, value)
                seen += 1
    assert seen > 0
