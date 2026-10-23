"""Поставщики шага решения и дублирующий запрос (BH-46, «Бюджет времени шага
решения и дублирующий запрос»; эталон — eval/providers.py::HedgedDecider).

Основной — gpt-oss-120b у Cloud.ru (reasoning_effort low, перепроверка —
medium), дублёр — Qwen3.6 у Яндекса (reasoning_effort none: с
размышлениями рвёт лимит токенов). Запрос — OpenAI-совместимый
/chat/completions со строгой схемой (decision_response_format).

Каждый вызов уходит основному; не ответил за задержку дубля (по типу
вызова: low — 3 с, на перепроверке medium дубля нет) или ответил быстрой
ошибкой, ошибкой настройки или не по схеме — тот же запрос уходит
дублёру. Побеждает первый годный ответ (прошёл parse_decision);
проигравший запрос отменяется — его токены не списываются. Срок — остаток
общего бюджета шага решения: ни одного годного к сроку — timeout, оба
упали — unavailable; цикл решения уходит в откат.

Повторов внутри звена нет: быстрая ошибка — это и есть повод для дубля.
"""

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
import structlog

from corp_ed.core.metrics import (
    DECISION_CALL_DURATION,
    DECISION_CALLS,
    DECISION_FAILOVER,
    DECISION_TOKENS,
)
from corp_ed.llm.errors import LLMError, classify
from corp_ed.llm.types import Completion, Message
from corp_ed.llm.yandex_headers import auth_headers
from corp_ed.llm.yandex_openai import URL as YANDEX_URL
from corp_ed.llm.yandex_openai import model_uri, parse_chat_response

logger = structlog.get_logger()

DECISION_MAX_TOKENS = 2000
"""Лимит выхода решения: 1000 у gpt-oss с medium обрезал JSON (BH-46)."""


class DecisionLeg(Protocol):
    """Звено шага решения: один вызов без повторов."""

    name: str
    model: str

    async def complete(
        self,
        messages: list[Message],
        *,
        effort: str,
        response_format: dict[str, Any],
        timeout: float,
    ) -> Completion: ...


class OpenAICompatibleLeg:
    """Звено с OpenAI-совместимым /chat/completions (Cloud.ru, Яндекс)."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        name: str,
        url: str,
        model: str,
        headers: dict[str, str],
        model_ref: str | None = None,
        effort: str | None = None,
    ) -> None:
        self._client = client
        self.name = name
        self.model = model
        self._url = url
        self._headers = headers
        self._model_ref = model_ref or model
        # Дублёр задаёт свой reasoning_effort (none), основной — по вызову.
        self._effort = effort

    async def complete(
        self,
        messages: list[Message],
        *,
        effort: str,
        response_format: dict[str, Any],
        timeout: float,
    ) -> Completion:
        payload = {
            "model": self._model_ref,
            "messages": [
                {"role": m.role.value, "content": m.content} for m in messages
            ],
            "temperature": 0,
            "max_tokens": DECISION_MAX_TOKENS,
            "response_format": response_format,
            "reasoning_effort": self._effort or effort,
        }
        started = time.perf_counter()
        try:
            response = await self._client.post(
                self._url,
                json=payload,
                headers=self._headers,
                timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            )
        except httpx.TimeoutException as exc:
            raise LLMError(
                f"{self.name}: timeout", retryable=True, kind="timeout"
            ) from exc
        except httpx.TransportError as exc:
            raise LLMError(f"{self.name}: transport: {exc}", retryable=True) from exc
        error = classify(response)
        if error is not None:
            raise error
        try:
            body = response.json()
        except ValueError as exc:
            raise LLMError(f"{self.name}: not JSON", retryable=False) from exc
        latency_ms = int((time.perf_counter() - started) * 1000)
        return parse_chat_response(body, model=self.model, latency_ms=latency_ms)


def cloudru_leg(
    client: httpx.AsyncClient, base_url: str, api_key: str, model: str
) -> OpenAICompatibleLeg:
    return OpenAICompatibleLeg(
        client,
        name="cloudru",
        url=f"{base_url.rstrip('/')}/chat/completions",
        model=model,
        headers={"Authorization": f"Bearer {api_key}"},
    )


def yandex_leg(
    client: httpx.AsyncClient, folder_id: str, api_key: str, model: str
) -> OpenAICompatibleLeg:
    """Qwen у Яндекса: тот же ключ и каталог, что у ответа Flash; без
    сохранения данных запроса (x-data-logging-enabled: false)."""
    return OpenAICompatibleLeg(
        client,
        name="yandex",
        url=YANDEX_URL,
        model=model,
        headers=auth_headers(api_key),
        model_ref=model_uri(folder_id, model),
        effort="none",
    )


@dataclass
class LegResult:
    """Исход одного звена в вызове: для журнала (decision_provider и т. п.)."""

    provider: str
    model: str
    effort: str
    outcome: str = "pending"
    status: int | None = None
    ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "effort": self.effort,
            "outcome": self.outcome,
            "status": self.status,
            "ms": self.ms,
        }


@dataclass
class Decided:
    """Итог вызова решения с дублем.

    completion — годный ответ (или ответ основного, если оба не по схеме);
    None — timeout (срок вышел) или unavailable (оба упали)."""

    completion: Completion | None
    winner: str = ""
    """primary / hedge / "" — годного ответа нет."""
    hedged: bool = False
    trigger: str = ""
    """Почему ушёл дубль: delay, error_fast, error_config, error_payment, invalid."""
    timeout: bool = False
    legs: list[LegResult] = field(default_factory=list)
    ms: int = 0

    @property
    def provider(self) -> str:
        """Звено, чей ответ взят; пусто — ответа нет."""
        if not self.legs or not self.winner:
            return ""
        return self.legs[0 if self.winner == "primary" else -1].provider


def _outcome(exc: LLMError) -> str:
    if exc.kind == "stream_broken":
        return "error_fast"
    return exc.kind


class HedgedDecider:
    """Вызов решения: основной, дублёр по задержке или ошибке, отмена
    проигравшего. delays — задержка дубля по reasoning_effort основного;
    None (или нет ключа) — дубля на таком вызове нет."""

    def __init__(
        self,
        primary: DecisionLeg | None,
        hedge: DecisionLeg | None,
        *,
        delays: dict[str, float | None],
        valid: Callable[[str], bool],
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if primary is None and hedge is None:
            raise ValueError("нужен хотя бы один поставщик решения")
        self.primary = primary
        self.hedge = hedge
        self.delays = delays
        self.valid = valid
        self._clock = clock

    async def decide(
        self,
        messages: list[Message],
        *,
        effort: str,
        call: str,
        response_format: dict[str, Any],
        budget: float,
    ) -> Decided:
        started = self._clock()
        deadline = started + max(budget, 0.0)
        result = Decided(completion=None)
        if budget <= 0:
            result.timeout = True
            return result
        first: DecisionLeg
        backup: DecisionLeg | None
        if self.primary is not None:
            first, backup = self.primary, self.hedge
        elif self.hedge is not None:
            first, backup = self.hedge, None
        else:
            raise ValueError("нужен хотя бы один поставщик решения")
        delay = self.delays.get(effort) if backup is not None else None
        legs: dict[asyncio.Task[Completion], tuple[str, DecisionLeg, LegResult]] = {}
        answers: dict[str, Completion] = {}

        def launch(role: str, leg: DecisionLeg) -> None:
            leg_effort = "none" if role == "hedge" else effort
            info = LegResult(provider=leg.name, model=leg.model, effort=leg_effort)
            result.legs.append(info)
            task = asyncio.create_task(
                leg.complete(
                    messages,
                    effort=effort,
                    response_format=response_format,
                    timeout=max(deadline - self._clock(), 0.01),
                )
            )
            legs[task] = (role, leg, info)

        def settle(task: asyncio.Task[Completion]) -> Completion | None:
            role, leg, info = legs[task]
            info.ms = int((self._clock() - started) * 1000)
            try:
                done = task.result()
            except LLMError as exc:
                info.outcome, info.status = _outcome(exc), exc.status
                logger.warning(
                    "decision_call_failed",
                    provider=leg.name,
                    outcome=info.outcome,
                    status=exc.status,
                )
                return None
            info.input_tokens = done.usage.input_tokens
            info.output_tokens = done.usage.output_tokens
            ok = self.valid(done.content)
            info.outcome = "ok" if ok else "invalid"
            answers[role] = done
            return done if ok else None

        launch("primary", first)
        try:
            while legs and result.completion is None:
                pending = {t for t in legs if not t.done()}
                if not pending:
                    break
                now = self._clock()
                if now >= deadline:
                    result.timeout = True
                    break
                waits = [deadline - now]
                if delay is not None and not result.hedged:
                    waits.append(max(started + delay - now, 0.0))
                done, _ = await asyncio.wait(
                    pending, timeout=min(waits), return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    won = settle(task)
                    if won is not None and result.completion is None:
                        result.completion = won
                        result.winner = legs[task][0]
                if result.completion is not None or backup is None or result.hedged:
                    continue
                failed = [legs[t][2] for t in done if legs[t][0] == "primary"]
                late = delay is not None and self._clock() >= started + delay
                if failed or late:
                    result.hedged = True
                    result.trigger = failed[0].outcome if failed else "delay"
                    if result.trigger in ("ok", "pending"):
                        result.trigger = "delay"
                    DECISION_FAILOVER.labels(_failover_reason(result.trigger)).inc()
                    launch("hedge", backup)
            if result.completion is None and not result.timeout:
                # Оба не по схеме — ответ основного: цикл запишет invalid.
                result.completion = answers.get("primary") or answers.get("hedge")
                if result.completion is not None:
                    result.winner = "primary" if "primary" in answers else "hedge"
        finally:
            for task, (_, _, info) in legs.items():
                if not task.done():
                    # Проигравший или опоздавший: отменить — токены не списываются.
                    task.cancel()
                    info.outcome = "timeout" if result.timeout else "cancelled"
                    info.ms = int((self._clock() - started) * 1000)
            for task in legs:
                with contextlib.suppress(asyncio.CancelledError, LLMError):
                    await task
        result.ms = int((self._clock() - started) * 1000)
        _record(result, call=call, budget=budget)
        return result


def _failover_reason(trigger: str) -> str:
    """Причина перехода ко второму звену (DECISION_FAILOVER_REASONS):
    задержка — hedge; таймаут основного до задержки — как быстрая ошибка."""
    if trigger == "delay":
        return "hedge"
    if trigger == "timeout":
        return "error_fast"
    return trigger


def _record(result: Decided, *, call: str, budget: float) -> None:
    for leg in result.legs:
        if leg.outcome == "cancelled":
            continue
        outcome = leg.outcome if leg.outcome != "pending" else "timeout"
        effort = leg.effort if leg.effort in ("low", "medium") else "none"
        DECISION_CALLS.labels(leg.provider, effort, call, outcome).inc()
        seconds = budget if outcome == "timeout" else leg.ms / 1000
        DECISION_CALL_DURATION.labels(leg.provider, effort).observe(seconds)
        DECISION_TOKENS.labels(leg.provider, "input").inc(leg.input_tokens)
        DECISION_TOKENS.labels(leg.provider, "output").inc(leg.output_tokens)
