"""Схема «решение до ответа» в сервисе ответов (BH-46, режим on).

Модель решения — подделка (tests/test_decision_loop.py), ответ Flash —
ScriptedStream по очереди: спекулятивный, затем ответ или общий.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import Material, QaLog, Tenant, User
from corp_ed.domain.small_talk import reply
from corp_ed.domain.types import AnswerOrigin, DecisionMode, NotFoundMode
from corp_ed.prompts.decision import EDITION_CAUTION
from corp_ed.prompts.faq import GENERAL_ANSWER_PREFIX, NOT_FOUND_ANSWER
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.services.faq_service import FaqService
from corp_ed.services.general_answer import CLARIFY_ADVICE
from tests.test_decision_loop import Decider, decision
from tests.test_search_step import (
    OTHER_AXIS,
    QUERY,
    QUERY_AXIS,
    QUESTION,
    QUESTION_AXIS,
    ScriptedStream,
    _document,
    _service,
)

ANSWER = "Закупки свыше 1 млн согласует финансовый директор [1]."


class Sink:
    """Ход ответа: этапы, origin, куски, сбросы — по порядку."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    async def stage(self, stage: str) -> None:
        self.events.append(("stage", stage))

    async def origin(self, origin: AnswerOrigin) -> None:
        self.events.append(("origin", origin))

    async def delta(self, text: str) -> None:
        self.events.append(("delta", text))

    async def reset(self) -> None:
        self.events.append(("reset", None))

    async def should_stop(self) -> bool:
        return False

    @property
    def shown(self) -> str:
        return "".join(str(v) for k, v in self.events if k == "delta")


class SpyDecider:
    """decide() подделки с записью того, что сотрудник видел к этому моменту."""

    def __init__(self, decider: Decider, sink: Sink) -> None:
        self.decider = decider
        self.sink = sink
        self.seen_before: list[list[tuple[str, Any]]] = []

    async def decide(self, *args: Any, **kwargs: Any) -> Any:
        self.seen_before.append(list(self.sink.events))
        return await self.decider(*args, **kwargs)


def _scheme(
    chunk_repo: ChunkRepository, llm: ScriptedStream, decider: Any
) -> FaqService:
    faq = _service(chunk_repo, llm)
    faq.decider = decider
    faq.decision_mode = DecisionMode.ON
    return faq


async def _log(session: AsyncSession) -> QaLog:
    return (await session.scalars(select(QaLog))).one()


async def _regulation(session: AsyncSession) -> None:
    await _document(
        session,
        "Регламент закупок",
        ["Закупки свыше 1 млн согласует финансовый директор."],
        QUESTION_AXIS,
    )


async def test_full_answer_is_the_speculative_one_and_nothing_leaks_before(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    await _regulation(session)
    sink = Sink()
    spy = SpyDecider(Decider(decision()), sink)
    llm = ScriptedStream(ANSWER)

    result = await _scheme(chunk_repo, llm, spy).answer_turn(
        QUESTION, employee, history=[], sink=sink
    )

    assert result.content == ANSWER
    assert result.origin is AnswerOrigin.DOCUMENTS
    # До вердикта — только этап «Ищу в документах…».
    assert spy.seen_before == [[("stage", "searching")]]
    assert sink.events[-2:] == [("stage", "writing"), ("delta", ANSWER)]
    # Один вызов Flash — спекулятивный, по faq-v2.7 без правила поиска.
    assert len(llm.calls) == 1
    assert "Дополнительный поиск" not in llm.calls[0][0].content

    entry = await _log(session)
    assert (entry.decision_mode, entry.decision_route) == ("on", "answer")
    assert entry.prompt_version == "faq-v2.7"
    assert entry.decision_calls == 1 and entry.decision_input_tokens == 300
    assert entry.decision is not None and entry.decision[0]["effort"] == "low"
    assert entry.decision_trace is not None
    assert entry.decision_trace["decision_speculated"] is True
    assert entry.decision_trace["decision_wasted"] == 0


async def test_other_subject_general_answer_with_the_note(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    await _regulation(session)
    other = decision(
        match="other",
        sufficiency="none",
        subject="Старт-ИИ-3",
        excerpts_subject="Старт-ИИ-1",
    )
    sink = Sink()
    general = f"{GENERAL_ANSWER_PREFIX}\nОбщие сведения о грантах."
    llm = ScriptedStream(ANSWER, general)

    result = await _scheme(
        chunk_repo, llm, SpyDecider(Decider(other, other), sink)
    ).answer_turn(QUESTION, employee, history=[], sink=sink)

    note = "В документах — «Старт-ИИ-1», а не «Старт-ИИ-3»."
    assert result.origin is AnswerOrigin.GENERAL_KNOWLEDGE
    assert result.content.startswith(f"{NOT_FOUND_ANSWER} {note}")
    assert result.content.endswith(CLARIFY_ADVICE)
    assert result.content.count(NOT_FOUND_ANSWER) == 1
    # Оговорка — первым куском после плашки, пометку модели срезает гейт.
    origin_at = sink.events.index(("origin", AnswerOrigin.GENERAL_KNOWLEDGE))
    assert sink.events[origin_at + 1] == ("delta", f"{note}\n")
    assert GENERAL_ANSWER_PREFIX not in sink.shown
    entry = await _log(session)
    assert entry.decision_route == "other_subject"
    assert entry.decision_trace is not None
    assert entry.decision_trace["decision_general_parallel"] is True


async def test_strict_mode_refuses_with_the_note(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    await _regulation(session)
    tenant_ctx.not_found_mode = NotFoundMode.STRICT.value
    await session.commit()
    other = decision(
        match="other",
        sufficiency="none",
        subject="Старт-ИИ-3",
        excerpts_subject="Старт-ИИ-1",
    )
    llm = ScriptedStream(ANSWER)
    faq = _scheme(chunk_repo, llm, SpyDecider(Decider(other, other), Sink()))

    answer = await faq.answer_turn(QUESTION, employee, history=[])

    assert answer.origin is AnswerOrigin.NONE
    assert answer.content == (
        f"{NOT_FOUND_ANSWER} В документах — «Старт-ИИ-1», а не «Старт-ИИ-3»."
        f"\n\n{CLARIFY_ADVICE}"
    )
    # Общий ответ в строгом режиме не зовётся — только спекулятивный.
    assert len(llm.calls) == 1


async def test_invalid_decision_falls_back_to_the_speculative_answer(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    await _regulation(session)
    llm = ScriptedStream(ANSWER)

    result = await _scheme(
        chunk_repo, llm, SpyDecider(Decider("{не json"), Sink())
    ).answer_turn(QUESTION, employee, history=[])

    assert result.content == ANSWER
    entry = await _log(session)
    assert entry.decision_trace is not None
    assert entry.decision_trace["decision_fallback"] == "invalid"
    assert entry.decision_trace["decision_invalid"] == 1


async def test_other_edition_answer_gets_the_caution_once(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    await _regulation(session)
    edition = decision(
        match="other_edition",
        sufficiency="full",
        subject="Регламент 2025",
        excerpts_subject="Регламент 2026",
    )
    model_text = (
        "В документах — «Регламент 2026», а не «Регламент 2025». "
        "Согласует финансовый директор [1]."
    )
    sink = Sink()
    llm = ScriptedStream(ANSWER, model_text)

    result = await _scheme(
        chunk_repo, llm, SpyDecider(Decider(edition), sink)
    ).answer_turn(QUESTION, employee, history=[], sink=sink)

    assert result.content.count(EDITION_CAUTION) == 1
    assert sink.shown.count(EDITION_CAUTION) == 1
    assert result.content.startswith(
        f"В документах — «Регламент 2026», а не «Регламент 2025»; {EDITION_CAUTION}."
    )
    # Второй вызов — промпт другой редакции, спекулятивный выброшен.
    assert len(llm.calls) == 2
    entry = await _log(session)
    assert entry.prompt_version == "edition-v1"
    assert entry.decision_trace is not None
    assert entry.decision_trace["decision_wasted_reason"] == "other_edition"


async def test_greeting_by_rule_in_on_mode_even_without_the_setting(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    llm = ScriptedStream()
    decider = Decider()
    faq = _scheme(chunk_repo, llm, SpyDecider(decider, Sink()))
    assert faq.small_talk_rule is False

    result = await faq.answer_turn("Привет!", employee, history=[])

    assert result.content == reply("greeting")
    assert decider.calls == [] and llm.calls == []


async def test_tenant_off_overrides_the_default_on(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    await _regulation(session)
    tenant_ctx.decision_mode = DecisionMode.OFF.value
    await session.commit()
    decider = Decider()
    llm = ScriptedStream(ANSWER)

    await _scheme(chunk_repo, llm, SpyDecider(decider, Sink())).answer_turn(
        QUESTION, employee, history=[]
    )

    assert decider.calls == []
    entry = await _log(session)
    assert entry.decision_mode == "off" and entry.decision_route is None


async def test_without_providers_on_is_off(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    await _regulation(session)
    llm = ScriptedStream(ANSWER)
    faq = _scheme(chunk_repo, llm, None)

    await faq.answer_turn(QUESTION, employee, history=[])

    assert (await _log(session)).decision_mode == "off"


async def test_partial_searches_more_and_answers_anew_by_stream(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    await _regulation(session)
    await _document(
        session,
        "Оргструктура",
        ["Финансовый директор — Петров Иван Сергеевич."],
        QUERY_AXIS,
    )
    partial = decision(sufficiency="partial", missing="ФИО", search_query=QUERY)
    final = "Согласует финансовый директор [1] — Петров Иван Сергеевич [2]."
    sink = Sink()
    llm = ScriptedStream(
        ANSWER, ["Согласует финансовый директор [1] — ", "Петров Иван Сергеевич [2]."]
    )

    result = await _scheme(
        chunk_repo, llm, SpyDecider(Decider(partial, decision()), sink)
    ).answer_turn(QUESTION, employee, history=[], sink=sink)

    assert result.content == final
    assert [s.title for s in result.sources] == ["Регламент закупок", "Оргструктура"]
    # Ответ заново — потоком, кусками; спекулятивный выброшен.
    assert sink.shown == final
    assert len(llm.calls) == 2
    entry = await _log(session)
    assert entry.decision_trace is not None
    assert entry.decision_trace["decision_queries"] == [QUERY]
    assert entry.decision_trace["decision_added"] == [1]
    assert entry.decision_trace["decision_wasted_reason"] == "shown_changed"
    assert entry.decision_calls == 2


ORG_LINES = [
    "— Финансовый директор — Кукушкина Ярослава Игоревна, кабинет 402, "
    "доб. 1120, kukushkina@example.ru",
    "— Бухгалтерия — главный бухгалтер Петров Иван Сергеевич, кабинет 405",
]


async def _org_chart(session: AsyncSession, *, superseded: bool = False) -> None:
    [chunk, _] = await _document(session, "Оргструктура", ORG_LINES, OTHER_AXIS)
    material = await session.get(Material, chunk.material_id)
    assert material is not None
    material.is_directory = True
    if superseded:
        material.superseded_at = datetime.now(UTC)
    await session.commit()


async def test_directory_hit_answers_with_the_contact_line(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    await _regulation(session)
    await _org_chart(session)
    partial = decision(
        sufficiency="partial", missing="ФИО", lookup_role="финансовый директор"
    )
    final = "Согласует финансовый директор Кукушкина Ярослава Игоревна [2]."
    llm = ScriptedStream(ANSWER, final)

    result = await _scheme(
        chunk_repo, llm, SpyDecider(Decider(partial), Sink())
    ).answer_turn(QUESTION, employee, history=[])

    assert result.content == final
    card = result.sources[-1]
    assert (card.title, card.heading_path) == ("Оргструктура", ["Справочник"])
    assert "Кукушкина Ярослава Игоревна, кабинет 402" in card.content
    # Ответ заново — с строкой контакта последним абзацем сообщения.
    user_message = llm.calls[1][-1].content
    assert user_message.endswith("укажи его в ответе со ссылкой на выдержку [2].")
    entry = await _log(session)
    assert entry.prompt_version == "faq-v2.7+contact-v1"
    assert entry.decision_trace is not None
    assert entry.decision_trace["decision_directory"] == "hit"
    assert entry.decision_trace["decision_directory_entries"] == 1


async def test_superseded_org_chart_is_not_the_directory(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    await _regulation(session)
    await _org_chart(session, superseded=True)
    partial = decision(
        sufficiency="partial", missing="ФИО", lookup_role="финансовый директор"
    )
    llm = ScriptedStream(ANSWER)

    await _scheme(chunk_repo, llm, SpyDecider(Decider(partial), Sink())).answer_turn(
        QUESTION, employee, history=[]
    )

    entry = await _log(session)
    assert entry.decision_trace is not None
    assert entry.decision_trace["decision_directory"] == ""
