"""Метрики вызовов модели ответа и эмбеддингов (BH-52).

Этап вызова (answer, general, condense, gaps_label) адаптер не знает —
его ставит вызывающий код через llm_stage(); по умолчанию answer. Пробы
ставят None: их считают corp_ed_llm_probe_total, а не вызовы.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from corp_ed.core.metrics import LLM_CALL_DURATION, LLM_CALLS
from corp_ed.llm.errors import LLMError
from corp_ed.llm.throttle import ThrottleBusyError

_stage: ContextVar[str | None] = ContextVar("llm_stage", default="answer")


@contextmanager
def llm_stage(stage: str | None) -> Iterator[None]:
    token = _stage.set(stage)
    try:
        yield
    finally:
        _stage.reset(token)


def current_stage() -> str | None:
    return _stage.get()


def outcome_of(exc: LLMError) -> str:
    """Исход вызова по ошибке — перечень LLM_OUTCOMES."""
    if isinstance(exc, ThrottleBusyError):
        return "busy"
    if exc.kind == "stream_broken":
        return "error_fast"
    return exc.kind


def record_call(
    provider: str, kind: str, stage: str | None, outcome: str, seconds: float
) -> None:
    if stage is None:
        return
    LLM_CALLS.labels(provider, kind, stage, outcome).inc()
    LLM_CALL_DURATION.labels(provider, kind).observe(seconds)
