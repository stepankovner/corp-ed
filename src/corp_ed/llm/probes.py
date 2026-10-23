"""Пробные вызовы главного цикла (BH-52).

Раз в 5 минут — короткий вызов Flash и эмбеддинг вопроса, раз в 15 —
резерв у Cloud.ru (если включён). Без персональных данных, в qa_log не
пишутся, кредиты клиентов не тратят, в corp_ed_llm_calls_total не идут —
у них свой счётчик corp_ed_llm_probe_total. Проба — тот же механизм, что
выход предохранителя из «открыт»: пока он не закрыт, проба идёт через 2
минуты, а не через 5; успех закрывает, неудача — ещё срок.

Живёт в процессе API, рядом с его предохранителями; воркер узнаёт о сбое
по своим вызовам.
"""

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable

import structlog

from corp_ed.core.metrics import LLM_PROBE, LLM_PROBE_FAILURES
from corp_ed.llm.embedding_gateway import EmbeddingGateway
from corp_ed.llm.errors import LLMError
from corp_ed.llm.fallback import CircuitBreaker
from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.observe import llm_stage, outcome_of
from corp_ed.llm.types import FinishReason, Message, Role

logger = structlog.get_logger()

PROBE_MESSAGES = [Message(role=Role.USER, content="Ответь одним словом: да")]
PROBE_TEXT = "проверка связи"
EVERY = 300.0
WHILE_OPEN = 120.0
RESERVE_EVERY = 900.0
TICK = 10.0


class _Target:
    def __init__(
        self,
        provider: str,
        kind: str,
        call: Callable[[], Awaitable[str]],
        every: float,
        breaker: CircuitBreaker | None,
    ) -> None:
        self.provider = provider
        self.kind = kind
        self.call = call
        self.every = every
        self.breaker = breaker
        self.last: float | None = None
        self.failures = 0

    def due(self, now: float) -> bool:
        if self.last is None:
            return True
        every = self.every
        if self.breaker is not None and self.breaker.state() != "closed":
            every = min(every, WHILE_OPEN)
        return now - self.last >= every


class Prober:
    def __init__(
        self,
        *,
        chat: LLMGateway | None = None,
        chat_breaker: CircuitBreaker | None = None,
        embeddings: EmbeddingGateway | None = None,
        embedding_breaker: CircuitBreaker | None = None,
        reserve: LLMGateway | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self.targets: list[_Target] = []
        if chat is not None:
            self.targets.append(
                _Target("yandex", "chat", _chat(chat), EVERY, chat_breaker)
            )
        if embeddings is not None:
            self.targets.append(
                _Target(
                    "yandex",
                    "embedding",
                    _embedding(embeddings),
                    EVERY,
                    embedding_breaker,
                )
            )
        if reserve is not None:
            self.targets.append(
                _Target("cloudru", "chat", _chat(reserve), RESERVE_EVERY, None)
            )

    async def run_once(self) -> None:
        now = self._clock()
        for target in self.targets:
            if target.due(now):
                target.last = now
                await self._probe(target)

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("llm_probe_loop_error")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=TICK)

    async def _probe(self, target: _Target) -> None:
        try:
            with llm_stage(None):
                outcome = await target.call()
        except LLMError as exc:
            outcome = outcome_of(exc)
            if target.breaker is not None and outcome != "busy":
                target.breaker.record_failure(exc.kind)
        else:
            if target.breaker is not None:
                target.breaker.record_success()
        LLM_PROBE.labels(target.provider, target.kind, outcome).inc()
        if outcome == "ok":
            target.failures = 0
        elif outcome != "busy":
            target.failures += 1
            logger.warning(
                "llm_probe_failed",
                provider=target.provider,
                kind=target.kind,
                outcome=outcome,
                streak=target.failures,
            )
        LLM_PROBE_FAILURES.labels(target.provider, target.kind).set(target.failures)


def _chat(gateway: LLMGateway) -> Callable[[], Awaitable[str]]:
    async def call() -> str:
        completion = await gateway.generate(PROBE_MESSAGES, temperature=0, max_tokens=5)
        return "filtered" if completion.finish_reason is FinishReason.FILTERED else "ok"

    return call


def _embedding(gateway: EmbeddingGateway) -> Callable[[], Awaitable[str]]:
    async def call() -> str:
        await gateway.embed_query(PROBE_TEXT)
        return "ok"

    return call
