"""Запасная модель ответа и предохранитель (BH-50, RESILIENCE-RESEARCH.md §2.3).

Яндекс — единая точка отказа продукта. Основная модель (Flash) зовётся с
короткими сроками (фабрика: 8 с до первого куска, 1 повтор только на
быструю ошибку); не ответила, пока сотрудник ещё ничего не видел, — тот
же список сообщений уходит запасной модели (gpt-oss-120b у Cloud.ru).
Начатый поток не подменяем: ответ уже на экране (решение команды, п. 3
контракта) — обрыв остаётся ошибкой, как раньше.

Предохранитель: N отказов подряд (таймауты и быстрые ошибки) или одна
ошибка настройки или оплаты — «открыт»: open_seconds все вызовы сразу в
резерв, основную не зовём и сроков не ждём; потом первый вызов — проба
основной: ответила — «закрыт», нет — ещё open_seconds. Состояние — в
памяти процесса (у API один процесс uvicorn).

content_filter — не отказ поставщика: ответ с FinishReason.FILTERED
возвращается как есть (BH-25), резерв не зовём. ThrottleBusyError —
перегрузка нашей очереди, не Яндекса: остаётся busy, предохранитель её
не считает (п. 5 контракта).
"""

import time
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from dataclasses import replace
from typing import Any

import structlog

from corp_ed.core.metrics import LLM_BREAKER_OPENED, LLM_BREAKER_STATE
from corp_ed.llm.errors import ErrorKind, LLMError
from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.throttle import ThrottleBusyError
from corp_ed.llm.types import Completion, Message

logger = structlog.get_logger()

BREAKER_OPEN = "breaker_open"
"""Причина ответа резерва: предохранитель открыт, основную не звали."""

_OPEN_AT_ONCE: frozenset[ErrorKind] = frozenset({"error_config", "error_payment"})


class CircuitBreaker:
    """Предохранитель Яндекса (BH-50 п. 6, BH-51): chat или embedding.

    Состояния — как в corp_ed_llm_breaker_state: closed (0), probe (1) —
    срок вышел, ждём исхода пробы, open (2). Пробой служит первый вызов
    после срока или пробный вызов (llm/probes.py, BH-52).
    """

    def __init__(
        self,
        failures: int = 3,
        open_seconds: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
        kind: str = "chat",
    ) -> None:
        self.kind = kind
        self.failures = failures
        self.open_seconds = open_seconds
        self._clock = clock
        self._streak = 0
        self._opened_at: float | None = None

    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at < self.open_seconds:
            return "open"
        return "probe"

    def is_open(self) -> bool:
        """Открыт и срок не вышел: основную не зовём."""
        return self.state() == "open"

    def allow_primary(self) -> bool:
        """Звать ли основную: закрыт — да; открыт — нет; срок вышел — проба."""
        state = self.state()
        self._publish(state)
        return state != "open"

    def record_success(self) -> None:
        if self._opened_at is not None:
            logger.info("llm_breaker_closed", kind=self.kind)
        self._streak = 0
        self._opened_at = None
        self._publish("closed")

    def record_failure(self, kind: ErrorKind) -> None:
        self._streak += 1
        if kind in _OPEN_AT_ONCE or self._streak >= self.failures:
            if not self.is_open():
                logger.warning(
                    "llm_breaker_opened",
                    kind=self.kind,
                    reason=kind,
                    streak=self._streak,
                )
                LLM_BREAKER_OPENED.labels("yandex", self.kind).inc()
            self._opened_at = self._clock()
            self._publish("open")

    def _publish(self, state: str) -> None:
        LLM_BREAKER_STATE.labels("yandex", self.kind).set(_STATE_VALUE[state])


_STATE_VALUE = {"closed": 0, "probe": 1, "open": 2}


class FallbackGateway(LLMGateway):
    """Основная модель с запасной и предохранителем (BH-50).

    В Completion ответа резерва — fallback_reason (класс отказа основной
    или breaker_open) и provider запасной: FaqService пишет их в журнал и
    метрику corp_ed_llm_fallback_total с этапом.

    Без резерва (reserve=None, ночные подписи пробелов) — только
    предохранитель: отказ основной остаётся ошибкой, а при открытом
    предохранителе вызов сразу падает, не дожидаясь срока.
    """

    def __init__(
        self,
        primary: LLMGateway,
        reserve: LLMGateway | None,
        breaker: CircuitBreaker,
    ) -> None:
        self.primary = primary
        self.reserve = reserve
        self.breaker = breaker
        self.model_name = primary.model_name

    def primary_down(self) -> bool:
        return self.breaker.is_open()

    def _no_reserve(self, error: LLMError | None) -> LLMError:
        return error or LLMError("primary model breaker is open", retryable=False)

    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
        response_format: dict[str, Any] | None = None,
    ) -> Completion:
        reason: str = BREAKER_OPEN
        error: LLMError | None = None
        if self.breaker.allow_primary():
            try:
                completion = await self.primary.generate(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                )
            except ThrottleBusyError:
                raise
            except LLMError as exc:
                self.breaker.record_failure(exc.kind)
                reason, error = exc.kind, exc
            else:
                self.breaker.record_success()
                return completion
        if self.reserve is None:
            raise self._no_reserve(error)
        logger.warning("llm_fallback", reason=reason, error=str(error or ""))
        completion = await self.reserve.generate(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
        )
        return replace(completion, fallback_reason=reason)

    async def stream(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
    ) -> AsyncGenerator[str | Completion, None]:
        reason: str = BREAKER_OPEN
        error: LLMError | None = None
        if self.breaker.allow_primary():
            shown = False
            try:
                async with aclosing(
                    self.primary.stream(
                        messages, temperature=temperature, max_tokens=max_tokens
                    )
                ) as items:
                    async for item in items:
                        if isinstance(item, Completion):
                            self.breaker.record_success()
                        else:
                            shown = True
                        yield item
                return
            except ThrottleBusyError:
                raise
            except LLMError as exc:
                self.breaker.record_failure(exc.kind)
                if shown:
                    # Ответ уже на экране — не подменяем (п. 3 контракта).
                    raise
                reason, error = exc.kind, exc
        if self.reserve is None:
            raise self._no_reserve(error)
        logger.warning("llm_fallback", reason=reason, error=str(error or ""))
        async with aclosing(
            self.reserve.stream(
                messages, temperature=temperature, max_tokens=max_tokens
            )
        ) as items:
            async for item in items:
                if isinstance(item, Completion):
                    yield replace(item, fallback_reason=reason)
                else:
                    yield item
