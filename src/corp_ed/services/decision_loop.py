"""Цикл «решение до ответа» до вердикта (BH-46, эталон —
eval/decision_loop.py::run_decided).

Здесь — от первого решения до маршрута: решение (low) и, если нужно,
перепроверка (medium) с дублем у второго поставщика; спекулятивный ответ
Flash параллельно с первым решением; справочник (BH-47) и доборы с новым
решением; бюджет времени и откат. Сам ответ сотруднику — потоком, общий
ответ, реплика — собирает FaqService по вердикту: ему нужен поток и журнал.

Умолчания продукта (решения Артёма 20–23.10): перепроверка отказов и
«хватает при роли» (R2, recheck="refusals+role"); общий ответ параллельно с
перепроверкой отказа (фаза 1); отсечение спекуляции — только gate; строка
контакта после справочника (A2); справочник до отказа — выключен.

Зависимости — функциями: цикл тестируется на подделках.
"""

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog

from corp_ed.domain.agent import MAX_EXCERPTS, MAX_EXTRA_SEARCHES
from corp_ed.domain.decision import (
    Decision,
    Route,
    is_refusal,
    lookup_role,
    parse_decision,
    route,
    search_query,
)
from corp_ed.domain.types import ChunkMatch
from corp_ed.llm.decision_client import Decided
from corp_ed.llm.types import Completion, Message
from corp_ed.prompts.decision import (
    build_decision_messages,
    contact_note,
    decision_response_format,
)

logger = structlog.get_logger()

RESPONSE_FORMAT = decision_response_format(reasoning=False, answer=False)

Decide = Callable[..., Awaitable[Decided]]
"""decide(messages, *, effort, call, response_format, budget) -> Decided."""
Search = Callable[[str, list[ChunkMatch]], Awaitable[list[ChunkMatch]]]
"""search(запрос, shown) — новые выдержки (которых нет в shown)."""
Answer = Callable[[], Coroutine[Any, Any, Completion]]


@dataclass
class DirectoryHit:
    """Что дал справочник (BH-47): выдержка «Справочник», записи и полное ли
    попадание (все запрошенные поля есть)."""

    match: ChunkMatch | None
    entries: list[Any]
    complete: bool


Directory = Callable[[str, str, str], Awaitable[DirectoryHit]]
"""directory(роль, чего не хватает, вопрос)."""


@dataclass
class Verdict:
    """Итог цикла до ответа и всё, что пишется в журнал (колонки decision_*)."""

    route: Route
    """Итоговый маршрут; при откате — ANSWER с decision=None (fallback)."""
    shown: list[ChunkMatch]
    decision: Decision | None = None
    fallback: str = ""
    """Откат: timeout, invalid, provider_error; пусто — решение есть."""
    spec: Completion | None = None
    """Спекулятивный ответ, годный для ANSWER (shown не менялся, не
    other_edition); выброшенные — в wasted."""
    general: Completion | None = None
    """Общий ответ, готовый параллельно с перепроверкой отказа."""
    contact: str = ""
    completions: list[Completion] = field(default_factory=list)
    """Вызовы Flash цикла (спекулятивный, общий параллельный): их токены —
    в кредиты, как у ответа."""
    decisions: list[dict[str, Any]] = field(default_factory=list)
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    decision_ms: int = 0
    recheck_ms: int = 0
    directory_ms: int = 0
    extra_search_ms: int = 0
    queries: list[str] = field(default_factory=list)
    added: list[int] = field(default_factory=list)
    invalid: int = 0
    exhausted: bool = False
    speculated: bool = False
    spec_skip: str = ""
    wasted: list[str] = field(default_factory=list)
    rechecked: list[str] = field(default_factory=list)
    """Причины перепроверок по порядку: refusal, role."""
    recheck_results: list[str] = field(default_factory=list)
    """refusal_confirmed / refusal_flipped / role_confirmed / role_flipped."""
    timeouts: list[str] = field(default_factory=list)
    """first / recheck / after_search."""
    directory: list[str] = field(default_factory=list)
    """hit / partial / miss по обращениям к справочнику."""
    directory_entries: int = 0
    extra_results: list[str] = field(default_factory=list)
    """new / empty / repeated / limit по доборам."""
    general_parallel: bool = False
    wasted_general: int = 0

    def trace(self) -> dict[str, Any]:
        """Журнал (qa_log.decision_trace): имена — как колонки стенда."""
        return {
            "decision_invalid": self.invalid,
            "decision_exhausted": self.exhausted,
            "decision_speculated": self.speculated,
            "decision_spec_skip": self.spec_skip,
            "decision_wasted": len(self.wasted),
            "decision_wasted_reason": ",".join(self.wasted),
            "decision_rechecked": len(self.rechecked),
            "decision_recheck_reason": ",".join(self.rechecked),
            "decision_timeout": ",".join(self.timeouts),
            "decision_fallback": self.fallback,
            "decision_queries": self.queries,
            "decision_added": self.added,
            "decision_directory": ",".join(self.directory),
            "decision_directory_entries": self.directory_entries,
            "decision_answer_mode": "contact" if self.contact else "",
            "decision_contact": self.contact,
            "decision_general_parallel": self.general_parallel,
            "decision_wasted_general": self.wasted_general,
            "decision_ms": self.decision_ms,
            "recheck_ms": self.recheck_ms,
            "directory_ms": self.directory_ms,
            "extra_search_ms": self.extra_search_ms,
        }


_REFUSALS = (Route.NOT_FOUND, Route.OTHER_SUBJECT)


def _recheck_reason(decision: Decision | None, step: Route) -> str | None:
    """Почему быстрое решение переспросить на medium (R2)."""
    if step in _REFUSALS or (
        decision is not None
        and (decision.sufficiency == "none" or is_refusal(decision))
    ):
        return "refusal"
    if (
        step is Route.ANSWER
        and decision is not None
        and decision.sufficiency == "full"
        and lookup_role(decision)
    ):
        return "role"
    return None


def _same(a: ChunkMatch, b: ChunkMatch) -> bool:
    return a.id == b.id or a.content == b.content


class DecisionLoop:
    def __init__(
        self,
        *,
        decide: Decide,
        search: Search,
        speculate: Callable[[list[ChunkMatch]], Coroutine[Any, Any, Completion]] | None,
        general: Answer | None,
        directory: Directory | None,
        budget: float,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.decide = decide
        self.search = search
        self.speculate = speculate
        self.general = general
        self.directory = directory
        self.budget = budget
        self._clock = clock
        self._spent = 0.0

    def _left(self) -> float:
        return self.budget - self._spent

    async def _call(
        self, verdict: Verdict, messages: list[Message], *, effort: str, call: str
    ) -> tuple[Decision | None, Decided]:
        started = self._clock()
        decided = await self.decide(
            messages,
            effort=effort,
            call=call,
            response_format=RESPONSE_FORMAT,
            budget=self._left(),
        )
        elapsed = self._clock() - started
        self._spent += elapsed
        ms = int(elapsed * 1000)
        if effort == "medium":
            verdict.recheck_ms += ms
        else:
            verdict.decision_ms += ms
        for leg in decided.legs:
            verdict.calls += 1
            verdict.input_tokens += leg.input_tokens
            verdict.output_tokens += leg.output_tokens
        record: dict[str, Any] = {
            "call": call,
            "effort": effort,
            "winner": decided.winner,
            "hedged": decided.hedged,
            "trigger": decided.trigger,
            "legs": [leg.as_dict() for leg in decided.legs],
        }
        decision: Decision | None = None
        if decided.completion is None:
            record["timeout" if decided.timeout else "unavailable"] = True
        else:
            decision = parse_decision(decided.completion.content)
            if decision is None:
                record["invalid"] = decided.completion.content[:500]
            else:
                record["decision"] = decision.model_dump(
                    exclude={"reasoning", "answer"}
                )
        verdict.decisions.append(record)
        return decision, decided

    async def _decide_chain(
        self,
        verdict: Verdict,
        messages: list[Message],
        *,
        call: str,
        can_search: bool,
    ) -> tuple[Decision | None, Route, str]:
        """Решение low и, при отказе или роли, перепроверка medium. Возвращает
        решение, маршрут и откат (пусто — решение есть)."""
        decision, decided = await self._call(verdict, messages, effort="low", call=call)
        if decided.completion is None:
            verdict.timeouts.append(call)
            return (
                None,
                Route.ANSWER,
                "timeout" if decided.timeout else "provider_error",
            )
        if decision is None:
            verdict.invalid += 1
            return None, Route.ANSWER, "invalid"
        directory = self.directory is not None
        step = route(decision, can_search=can_search, directory=directory)
        reason = _recheck_reason(decision, step)
        if reason is None:
            return decision, step, ""
        verdict.rechecked.append(reason)
        general_task: asyncio.Task[Completion] | None = None
        if reason == "refusal" and step in _REFUSALS and self.general is not None:
            # Общий ответ — одновременно с перепроверкой (фаза 1): подтвердила
            # отказ — он и пойдёт сотруднику; перевернула — выброшен.
            general_task = asyncio.create_task(self.general())
            verdict.general_parallel = True
        try:
            again, decided = await self._call(
                verdict, messages, effort="medium", call="recheck"
            )
        except BaseException:
            if general_task is not None:
                general_task.cancel()
            raise
        if general_task is not None:
            with contextlib.suppress(Exception):
                completion = await general_task
                verdict.completions.append(completion)
                verdict.general = completion
        if decided.completion is None:
            # Перепроверка не успела — берём быстрое решение.
            verdict.timeouts.append("recheck")
            verdict.recheck_results.append(f"{reason}_confirmed")
            return decision, step, ""
        if again is None:
            verdict.invalid += 1
            verdict.recheck_results.append(f"{reason}_confirmed")
            return decision, step, ""
        new_step = route(again, can_search=can_search, directory=directory)
        verdict.recheck_results.append(
            f"{reason}_{'confirmed' if new_step is step else 'flipped'}"
        )
        return again, new_step, ""

    async def run(
        self,
        question: str,
        initial: Sequence[ChunkMatch],
        *,
        spec_skip: str = "",
    ) -> Verdict:
        shown = list(initial)[:MAX_EXCERPTS]
        verdict = Verdict(route=Route.NOT_FOUND, shown=shown, spec_skip=spec_skip)
        spec_task: asyncio.Task[Completion] | None = None
        if self.speculate is not None and shown and not spec_skip:
            spec_task = asyncio.create_task(self.speculate(list(shown)))
            verdict.speculated = True
        searches = 0
        decision: Decision | None = None
        step = Route.NOT_FOUND
        try:
            while True:
                can_search = searches < MAX_EXTRA_SEARCHES and len(shown) < MAX_EXCERPTS
                messages = build_decision_messages(
                    question,
                    shown,
                    searched=list(zip(verdict.queries, verdict.added, strict=True)),
                )
                call = "first" if not verdict.decisions else "after_search"
                decision, step, fallback = await self._decide_chain(
                    verdict, messages, call=call, can_search=can_search
                )
                if fallback:
                    verdict.fallback = fallback
                    break
                assert decision is not None  # noqa: S101 — откат вышел выше
                if step is Route.DIRECTORY and self.directory is not None:
                    role = lookup_role(decision)
                    started = self._clock()
                    hit = await self.directory(role or "", decision.missing, question)
                    verdict.directory_ms += int((self._clock() - started) * 1000)
                    added = hit.match is not None and not any(
                        _same(hit.match, m) for m in shown
                    )
                    if added and hit.match is not None:
                        # Записи — выдержкой сверх лимита: короткие, резать нельзя.
                        shown.append(hit.match)
                        verdict.directory_entries = len(hit.entries)
                    if hit.match is not None and hit.complete:
                        # Полное попадание — сразу ответ, со строкой контакта
                        # (A2; при 3+ записях contact_note пуст).
                        verdict.directory.append("hit")
                        number = next(
                            k for k, m in enumerate(shown, 1) if _same(m, hit.match)
                        )
                        verdict.contact = contact_note(hit.entries, number)
                        step = Route.ANSWER
                        break
                    verdict.directory.append("partial" if hit.match else "miss")
                    can_search = (
                        searches < MAX_EXTRA_SEARCHES and len(shown) <= MAX_EXCERPTS
                    )
                    step = route(decision, can_search=can_search)
                if step is Route.SEARCH:
                    query = search_query(decision)
                    if query is None or query.casefold() in {
                        q.casefold() for q in verdict.queries
                    }:
                        verdict.exhausted = True
                        verdict.extra_results.append("repeated")
                        step = route(decision, can_search=False)
                        break
                    searches += 1
                    verdict.queries.append(query)
                    started = self._clock()
                    room = max(0, MAX_EXCERPTS - len(shown))
                    found = await self.search(query, list(shown))
                    verdict.extra_search_ms += int((self._clock() - started) * 1000)
                    new = [m for m in found if not any(_same(m, s) for s in shown)]
                    new = new[:room]
                    verdict.added.append(len(new))
                    if new:
                        shown.extend(new)
                        verdict.extra_results.append("new")
                        continue
                    verdict.exhausted = True
                    verdict.extra_results.append("empty")
                    step = route(decision, can_search=False)
                elif (
                    decision.sufficiency == "partial"
                    and decision.kind == "question"
                    and decision.match != "other"
                    and not can_search
                ):
                    verdict.exhausted = True
                    verdict.extra_results.append("limit")
                break
            verdict.route = step
            verdict.decision = decision
            if spec_task is not None:
                task, spec_task = spec_task, None
                try:
                    spec = await task
                except Exception as exc:  # noqa: BLE001 — ответ соберём заново
                    logger.warning("decision_spec_failed", error=type(exc).__name__)
                else:
                    verdict.completions.append(spec)
                    reason = self._wasted(verdict, initial)
                    if reason:
                        verdict.wasted.append(reason)
                    else:
                        verdict.spec = spec
            if verdict.general is not None and verdict.route not in _REFUSALS:
                # Перепроверка перевернула отказ — общий ответ не нужен.
                verdict.general = None
                verdict.wasted_general += 1
            if verdict.fallback and not shown:
                verdict.route = Route.NOT_FOUND
            return verdict
        finally:
            if spec_task is not None:
                spec_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await spec_task

    @staticmethod
    def _wasted(verdict: Verdict, initial: Sequence[ChunkMatch]) -> str:
        """Почему спекулятивный ответ не годится (пусто — годится)."""
        if verdict.route is Route.SMALL_TALK:
            return "small_talk"
        if verdict.route is Route.OTHER_SUBJECT:
            return "other_subject"
        if verdict.route is Route.NOT_FOUND:
            return "not_found"
        if len(verdict.shown) != len(list(initial)[:MAX_EXCERPTS]):
            return "shown_changed"
        if verdict.decision is not None and verdict.decision.match == "other_edition":
            return "other_edition"
        return ""
