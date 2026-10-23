"""Теневой режим схемы «решение до ответа» (BH-46).

Сотрудник получает ответ конвейера как сейчас — без этапа «Ищу в
документах…» и без задержки; цикл схемы идёт в фоне и дописывает журнал.
При ANSWER без добора ответ конвейера и есть ответ схемы — один вызов Flash.
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.domain.models import QaLog, Tenant, User
from corp_ed.domain.types import AnswerOrigin, DecisionMode
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.services.faq_service import FaqService
from tests.test_decision_loop import Decider, decision
from tests.test_decision_scheme import (
    ANSWER,
    Sink,
    SpyDecider,
    _org_chart,
    _regulation,
)
from tests.test_search_step import QUERY, QUESTION, ScriptedStream, _service


def _shadow(
    chunk_repo: ChunkRepository,
    llm: ScriptedStream,
    decider: Decider,
    maker: async_sessionmaker[AsyncSession],
    *,
    search_step: bool = False,
) -> FaqService:
    faq = _service(chunk_repo, llm, search_step=search_step)
    faq.decider = SpyDecider(decider, Sink())  # type: ignore[assignment]
    faq.decision_mode = DecisionMode.SHADOW
    faq.shadow_sessions = maker
    return faq


async def _trace(session: AsyncSession, faq: FaqService) -> QaLog:
    await asyncio.gather(*faq.shadow_tasks)
    session.expire_all()
    return (await session.scalars(select(QaLog))).one()


async def test_pipeline_answer_is_the_scheme_answer_one_flash_call(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    await _regulation(session)
    llm = ScriptedStream(ANSWER)
    faq = _shadow(chunk_repo, llm, Decider(decision()), session_maker)
    sink = Sink()

    result = await faq.answer_turn(QUESTION, employee, history=[], sink=sink)

    # Сотруднику — ответ конвейера, потоком, как сейчас.
    assert result.content == ANSWER
    assert result.origin is AnswerOrigin.DOCUMENTS
    assert sink.shown == ANSWER
    entry = await _trace(session, faq)
    assert len(llm.calls) == 1
    assert (entry.decision_mode, entry.decision_route) == ("shadow", "answer")
    assert entry.decision_trace is not None
    assert entry.decision_trace["decision_shadow_source"] == "pipeline"
    assert entry.decision_trace["decision_shadow_agree"] is True
    assert entry.decision_trace["decision_shadow_not_found"] is False
    assert entry.decision_calls == 1
    # Кредиты — только за ответ конвейера.
    assert entry.input_tokens == 100


async def test_scheme_refusal_is_logged_without_calls(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    await _regulation(session)
    nothing = decision(sufficiency="none")
    llm = ScriptedStream(ANSWER)
    faq = _shadow(chunk_repo, llm, Decider(nothing, nothing), session_maker)

    result = await faq.answer_turn(QUESTION, employee, history=[])

    assert result.content == ANSWER
    entry = await _trace(session, faq)
    assert len(llm.calls) == 1
    assert entry.decision_route == "not_found"
    assert entry.decision_trace is not None
    assert entry.decision_trace["decision_shadow_source"] == "refusal"
    assert entry.decision_trace["decision_shadow_agree"] is False
    assert entry.decision_trace["decision_pipeline_not_found"] is False


async def test_directory_hit_makes_its_own_answer_call(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    await _regulation(session)
    await _org_chart(session)
    partial = decision(
        sufficiency="partial", missing="ФИО", lookup_role="финансовый директор"
    )
    llm = ScriptedStream(ANSWER, "Кукушкина Ярослава Игоревна [2].")
    faq = _shadow(chunk_repo, llm, Decider(partial), session_maker)

    result = await faq.answer_turn(QUESTION, employee, history=[])

    assert result.content == ANSWER
    entry = await _trace(session, faq)
    assert len(llm.calls) == 2
    assert entry.decision_trace is not None
    assert entry.decision_trace["decision_shadow_source"] == "own"
    assert entry.decision_trace["decision_directory"] == "hit"
    assert entry.decision_trace["decision_shadow_answer_calls"] == 1
    # Второй вызов схемы в кредиты сотрудника не идёт.
    assert entry.input_tokens == 100


async def test_search_request_from_the_pipeline_is_not_a_ready_answer(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    await _regulation(session)
    # Конвейер с BH-43: первый вызов — строка поиска, второго не нашлось.
    llm = ScriptedStream(
        f"Дополнительный поиск: {QUERY}",
        "В документах компании ответа нет.\\nОбщее.",
        ANSWER,
    )
    faq = _shadow(chunk_repo, llm, Decider(decision()), session_maker, search_step=True)

    await faq.answer_turn(QUESTION, employee, history=[])

    entry = await _trace(session, faq)
    assert entry.decision_trace is not None
    assert entry.decision_trace["decision_shadow_source"] == "own"


async def test_rule_in_shadow_is_only_logged(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    decider = Decider()
    llm = ScriptedStream("В документах компании ответа нет.\\nЗдравствуйте!")
    faq = _shadow(chunk_repo, llm, decider, session_maker)

    result = await faq.answer_turn("Привет!", employee, history=[])

    # Ответ — конвейера (общий), схема записала правило.
    assert result.origin is AnswerOrigin.GENERAL_KNOWLEDGE
    entry = await _trace(session, faq)
    assert decider.calls == []
    assert entry.decision_trace is not None
    assert entry.decision_trace["decision_by_rule"] is True
    assert entry.decision_trace["decision_shadow_source"] == "rule"
