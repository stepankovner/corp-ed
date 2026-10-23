"""Цикл «решение до ответа» до вердикта (BH-46, «Тесты у вас»).

Модель решения, поиск, спекулятивный и общий ответ — подделки.
"""

import json
from typing import Any
from uuid import uuid4

from corp_ed.domain.decision import Route
from corp_ed.domain.types import ChunkMatch
from corp_ed.llm.decision_client import Decided, LegResult
from corp_ed.llm.types import Completion, FinishReason, Message, Usage
from corp_ed.services.decision_loop import DecisionLoop, DirectoryHit

QUESTION = "Кто согласует закупку на 2 млн?"


def _match(text: str) -> ChunkMatch:
    return ChunkMatch(
        id=uuid4(),
        material_id=uuid4(),
        title="Регламент",
        content=text,
        heading_path=[],
        distance=0.4,
        position=0,
    )


def _completion(text: str) -> Completion:
    return Completion(
        content=text,
        finish_reason=FinishReason.COMPLETED,
        usage=Usage(input_tokens=100, output_tokens=10),
        model_version="v",
        model="m",
        latency_ms=1,
    )


def decision(**fields: Any) -> str:
    base = {
        "kind": "question",
        "subject": "закупка",
        "excerpts_subject": "закупка",
        "match": "same",
        "sufficiency": "full",
        "missing": "—",
        "search_query": "—",
        "lookup_role": "—",
    }
    return json.dumps({**base, **fields}, ensure_ascii=False)


class Decider:
    """Ответы решения по очереди; None — таймаут."""

    def __init__(self, *texts: str | None) -> None:
        self.texts = list(texts)
        self.calls: list[tuple[str, str, list[Message]]] = []

    async def __call__(
        self,
        messages: list[Message],
        *,
        effort: str,
        call: str,
        response_format: dict[str, Any],
        budget: float,
    ) -> Decided:
        self.calls.append((effort, call, messages))
        text = self.texts.pop(0)
        leg = LegResult(provider="cloudru", model="gpt-oss", effort=effort)
        if text is None:
            leg.outcome = "timeout"
            return Decided(completion=None, timeout=True, legs=[leg])
        leg.outcome = "ok"
        leg.input_tokens, leg.output_tokens = 300, 50
        return Decided(completion=_completion(text), winner="primary", legs=[leg])


class Tools:
    def __init__(self, found: list[list[ChunkMatch]] | None = None) -> None:
        self.found = found or []
        self.queries: list[str] = []
        self.specs = 0
        self.generals = 0

    async def search(self, query: str, shown: list[ChunkMatch]) -> list[ChunkMatch]:
        self.queries.append(query)
        return self.found.pop(0) if self.found else []

    async def speculate(self, shown: list[ChunkMatch]) -> Completion:
        self.specs += 1
        return _completion("Согласует финансовый директор [1].")

    async def general(self) -> Completion:
        self.generals += 1
        return _completion("В документах компании ответа нет.\nОбщее.")


def _loop(decider: Decider, tools: Tools, *, directory: Any = None) -> DecisionLoop:
    return DecisionLoop(
        decide=decider,
        search=tools.search,
        speculate=tools.speculate,
        general=tools.general,
        directory=directory,
        budget=6.0,
    )


SHOWN = [_match("Закупки свыше 1 млн согласует финансовый директор.")]


async def test_full_answer_takes_the_speculative_one() -> None:
    decider, tools = Decider(decision()), Tools()

    verdict = await _loop(decider, tools).run(QUESTION, SHOWN)

    assert verdict.route is Route.ANSWER
    assert verdict.spec is not None and verdict.wasted == []
    assert [c[0] for c in decider.calls] == ["low"]
    assert verdict.calls == 1 and verdict.input_tokens == 300


async def test_partial_searches_then_decides_again_with_the_history() -> None:
    extra = [_match("Финансовый директор — Петров Иван Сергеевич.")]
    decider = Decider(
        decision(
            sufficiency="partial", missing="ФИО", search_query="финансовый директор ФИО"
        ),
        decision(),
    )
    tools = Tools([extra])

    verdict = await _loop(decider, tools).run(QUESTION, SHOWN)

    assert tools.queries == ["финансовый директор ФИО"]
    assert verdict.queries == ["финансовый директор ФИО"] and verdict.added == [1]
    _, call, messages = decider.calls[1]
    assert call == "after_search"
    assert "Уже искали: «финансовый директор ФИО» — новых выдержек: 1" in (
        messages[-1].content
    )
    assert verdict.route is Route.ANSWER
    assert len(verdict.shown) == 2
    # Спекулятивный ответ не видел новой выдержки — выброшен.
    assert verdict.spec is None and verdict.wasted == ["shown_changed"]


async def test_same_query_twice_is_not_searched_again() -> None:
    partial = decision(
        sufficiency="partial", missing="ФИО", search_query="ФИО директора"
    )
    decider = Decider(partial, partial)
    tools = Tools([[_match("Новая выдержка.")]])

    verdict = await _loop(decider, tools).run(QUESTION, SHOWN)

    assert tools.queries == ["ФИО директора"]
    assert verdict.exhausted
    assert verdict.extra_results == ["new", "repeated"]
    assert verdict.route is Route.ANSWER


async def test_no_third_search() -> None:
    def partial(n: int) -> str:
        return decision(
            sufficiency="partial", missing="ФИО", search_query=f"запрос {n}"
        )

    decider = Decider(partial(1), partial(2), partial(3))
    tools = Tools([[_match("А.")], [_match("Б.")], [_match("В.")]])

    verdict = await _loop(decider, tools).run(QUESTION, SHOWN)

    assert tools.queries == ["запрос 1", "запрос 2"]
    assert verdict.extra_results == ["new", "new", "limit"]
    assert verdict.route is Route.ANSWER


async def test_refusal_is_rechecked_with_the_general_answer_in_parallel() -> None:
    nothing = decision(sufficiency="none")
    decider, tools = Decider(nothing, nothing), Tools()

    verdict = await _loop(decider, tools).run(QUESTION, SHOWN)

    assert [c[0] for c in decider.calls] == ["low", "medium"]
    assert verdict.route is Route.NOT_FOUND
    assert verdict.rechecked == ["refusal"]
    assert verdict.recheck_results == ["refusal_confirmed"]
    assert verdict.general is not None and verdict.general_parallel
    assert verdict.wasted == ["not_found"]


async def test_recheck_flips_the_refusal_and_drops_the_general_answer() -> None:
    decider, tools = Decider(decision(sufficiency="none"), decision()), Tools()

    verdict = await _loop(decider, tools).run(QUESTION, SHOWN)

    assert verdict.route is Route.ANSWER
    assert verdict.recheck_results == ["refusal_flipped"]
    assert verdict.general is None and verdict.wasted_general == 1
    assert verdict.spec is not None


async def test_full_with_a_role_is_rechecked_r2() -> None:
    role = decision(lookup_role="финансовый директор")
    decider, tools = Decider(role, decision()), Tools()

    verdict = await _loop(decider, tools).run(QUESTION, SHOWN)

    assert verdict.rechecked == ["role"]
    assert verdict.recheck_results == ["role_confirmed"]
    assert tools.generals == 0


async def test_invalid_json_falls_back_to_the_speculative_answer() -> None:
    decider, tools = Decider("{не json"), Tools()

    verdict = await _loop(decider, tools).run(QUESTION, SHOWN)

    assert verdict.fallback == "invalid" and verdict.invalid == 1
    assert verdict.route is Route.ANSWER and verdict.decision is None
    assert verdict.spec is not None


async def test_timeout_falls_back_and_is_logged() -> None:
    decider, tools = Decider(None), Tools()

    verdict = await _loop(decider, tools).run(QUESTION, SHOWN)

    assert verdict.fallback == "timeout"
    assert verdict.timeouts == ["first"]
    assert verdict.spec is not None


async def test_timeout_without_excerpts_is_not_found() -> None:
    decider, tools = Decider(None), Tools()

    verdict = await _loop(decider, tools).run(QUESTION, [])

    assert verdict.route is Route.NOT_FOUND
    assert tools.specs == 0


async def test_other_edition_drops_the_speculative_answer() -> None:
    edition = decision(
        match="other_edition", sufficiency="none", subject="Регламент 2025"
    )
    # sufficiency=none перепроверяется и при другой редакции (как в eval);
    # маршрут — ответ, общий ответ параллельно не запускался.
    decider, tools = Decider(edition, edition), Tools()

    verdict = await _loop(decider, tools).run(QUESTION, SHOWN)

    assert verdict.route is Route.ANSWER
    assert verdict.wasted == ["other_edition"] and verdict.spec is None
    assert tools.generals == 0


async def test_gate_skips_the_speculative_answer() -> None:
    decider, tools = Decider(decision()), Tools()

    verdict = await _loop(decider, tools).run(QUESTION, SHOWN, spec_skip="gate")

    assert tools.specs == 0 and not verdict.speculated
    assert verdict.spec_skip == "gate"


async def test_small_talk_route() -> None:
    decider, tools = Decider(decision(kind="small_talk")), Tools()

    verdict = await _loop(decider, tools).run("Ну и денёк", SHOWN)

    assert verdict.route is Route.SMALL_TALK
    assert verdict.wasted == ["small_talk"]


async def test_directory_hit_answers_with_a_contact_line() -> None:
    card = _match("— Финансовый директор — Петров Иван Сергеевич, кабинет 402")

    class Entry:
        def text(self) -> str:
            return "Финансовый директор — Петров Иван Сергеевич, кабинет 402"

    async def directory(role: str, missing: str, question: str) -> DirectoryHit:
        assert role == "финансовый директор"
        return DirectoryHit(match=card, entries=[Entry()], complete=True)

    partial = decision(
        sufficiency="partial", missing="ФИО", lookup_role="финансовый директор"
    )
    decider, tools = Decider(partial), Tools()

    verdict = await _loop(decider, tools, directory=directory).run(QUESTION, SHOWN)

    assert verdict.route is Route.ANSWER
    assert verdict.directory == ["hit"]
    assert verdict.shown[-1] is card
    assert "[2]" in verdict.contact
    assert verdict.wasted == ["shown_changed"]
    assert tools.queries == []


async def test_directory_partial_goes_to_vector_search() -> None:
    card = _match("— Отдел персонала — Иванова Анна, кабинет 101")

    async def directory(role: str, missing: str, question: str) -> DirectoryHit:
        return DirectoryHit(match=card, entries=[], complete=False)

    partial = decision(
        sufficiency="partial",
        missing="почта",
        search_query="почта отдела персонала",
        lookup_role="отдел персонала",
    )
    decider, tools = Decider(partial, decision()), Tools([[_match("hr@example.ru")]])

    verdict = await _loop(decider, tools, directory=directory).run(QUESTION, SHOWN)

    assert verdict.directory == ["partial"]
    assert tools.queries == ["почта отдела персонала"]
    assert verdict.route is Route.ANSWER
    assert verdict.contact == ""
