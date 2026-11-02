"""Decision-mode safety contracts without a database or provider calls.

Can run separately with --noconftest; repository reads and providers are fakes,
but the service, decision loop, output gates and transaction release are real.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from corp_ed.domain.decision import Decision, Route
from corp_ed.domain.types import AnswerOrigin, ChunkMatch, NotFoundMode, Retriever
from corp_ed.llm.decision_client import Decided
from corp_ed.llm.types import Completion, FinishReason, Usage
from corp_ed.prompts.faq import GENERAL_ANSWER_PREFIX, SMALL_TALK_PREFIX
from corp_ed.services.decision_loop import Verdict
from corp_ed.services.faq_service import FaqService
from corp_ed.services.general_answer import REFUSAL_ANSWER

QUESTION = "Who approves purchases?"
ANSWER = "The director approves [1]."
DECISION = Decision(
    kind="question",
    subject="purchases",
    excerpts_subject="purchases",
    match="same",
    sufficiency="full",
    missing="—",
    search_query="—",
)
SHOWN = [
    ChunkMatch(
        id=uuid4(),
        material_id=uuid4(),
        content=ANSWER,
        title="Policy",
        heading_path=[],
        distance=0.4,
        position=0,
    )
]


def completion(text=ANSWER, reason=FinishReason.COMPLETED):
    return Completion(
        content=text,
        finish_reason=reason,
        usage=Usage(input_tokens=100, output_tokens=10),
        model="fake",
        model_version="fake-v1",
        latency_ms=1,
    )


class Sink:
    def __init__(self, *, stopped=False, stop_after_delta=False):
        self.stopped = stopped
        self.stop_after_delta = stop_after_delta
        self.deltas = []
        self.origins = []
        self.resets = 0

    async def stage(self, stage):
        pass

    async def origin(self, origin):
        self.origins.append(origin)

    async def delta(self, text):
        self.deltas.append(text)
        if self.stop_after_delta:
            self.stopped = True

    async def reset(self):
        self.resets += 1

    async def should_stop(self):
        return self.stopped


def service(monkeypatch, answer=None):
    state = SimpleNamespace(transaction=False)

    async def commit():
        state.transaction = False

    async def directories():
        state.transaction = True
        return []

    session = Mock(
        in_transaction=lambda: state.transaction, commit=AsyncMock(side_effect=commit)
    )
    monkeypatch.setattr(
        "corp_ed.services.faq_service.MaterialRepository",
        lambda session: SimpleNamespace(directories=directories),
    )
    llm = Mock(
        generate=AsyncMock(return_value=answer or completion()),
        model_name="fake",
    )
    decider = Mock(
        decide=AsyncMock(
            return_value=Decided(completion=completion(DECISION.model_dump_json()))
        )
    )
    faq = FaqService(
        chunk_repo=Mock(),
        qa_log_repo=Mock(),
        tenant_repo=Mock(),
        glossary_repo=Mock(),
        credits=Mock(),
        embedding_gateway=Mock(),
        llm_gateway=llm,
        session=session,
        limit=5,
        max_distance=0.59,
        context_max_tokens=6000,
        temperature=0.0,
        retriever=Retriever.VECTOR,
        fulltext_weight=1.0,
        decider=decider,
    )
    return faq, state


async def decided(faq, sink, mode=NotFoundMode.GENERAL):
    return await faq._decided(
        QUESTION, SHOWN, mode, viewer=uuid4(), attached=[], nearest=0.4, sink=sink
    )


async def test_directory_read_releases_transaction_before_both_providers(monkeypatch):
    faq, state = service(monkeypatch)
    transactions = []

    async def decide(*args, **kwargs):
        transactions.append(state.transaction)
        return Decided(completion=completion(DECISION.model_dump_json()))

    async def generate(*args, **kwargs):
        transactions.append(state.transaction)
        return completion()

    faq.decider.decide.side_effect = decide
    faq.llm_gateway.generate.side_effect = generate
    result = await decided(faq, Sink())

    assert result.content == ANSWER
    assert transactions == [False, False]
    faq.session.commit.assert_awaited_once()


@pytest.mark.parametrize("text", ["", "Filtered provider content"])
@pytest.mark.parametrize("mode", list(NotFoundMode))
async def test_filtered_speculation_never_reaches_user_and_is_charged_once(
    monkeypatch, text, mode
):
    filtered = completion(text, FinishReason.FILTERED)
    faq, _ = service(monkeypatch, filtered)
    sink = Sink()

    result = await decided(faq, sink, mode)

    assert result.origin is AnswerOrigin.NONE
    assert result.content == REFUSAL_ANSWER
    assert result.sources == []
    assert sink.deltas == []
    assert result.completions == [filtered]
    faq.llm_gateway.generate.assert_awaited_once()


async def test_stop_during_speculation_suppresses_output_preserves_completed_usage(
    monkeypatch,
):
    faq, _ = service(monkeypatch)
    sink = Sink()
    spec = completion()

    async def generate(*args, **kwargs):
        sink.stopped = True
        return spec

    faq.llm_gateway.generate.side_effect = generate
    result = await decided(faq, sink)

    assert result.stopped
    assert result.content == ""
    assert sink.deltas == []
    assert result.completions == [spec]
    faq.llm_gateway.generate.assert_awaited_once()


async def test_stop_before_decision_does_not_start_providers(monkeypatch):
    faq, _ = service(monkeypatch)
    result = await decided(faq, Sink(stopped=True))

    assert result.stopped
    assert result.content == ""
    assert result.completions == []
    faq.decider.decide.assert_not_awaited()
    faq.llm_gateway.generate.assert_not_awaited()


@pytest.mark.parametrize("route", [Route.ANSWER, Route.NOT_FOUND, Route.SMALL_TALK])
async def test_stop_before_ready_branch_never_emits_or_starts_a_call(
    monkeypatch, route
):
    faq, _ = service(monkeypatch)
    faq.llm_gateway.generate.return_value = completion(f"{SMALL_TALK_PREFIX}\nHello.")
    sink = Sink(stopped=True)
    verdict = Verdict(route=route, shown=SHOWN, spec=completion(), general=completion())
    branch = {
        Route.ANSWER: faq._scheme_answer,
        Route.NOT_FOUND: faq._scheme_not_found,
        Route.SMALL_TALK: faq._scheme_small_talk,
    }[route]

    result = await branch(QUESTION, verdict, NotFoundMode.GENERAL, sink)

    assert result.stopped
    assert result.content == ""
    assert sink.deltas == []
    faq.llm_gateway.generate.assert_not_awaited()


@pytest.mark.parametrize("small_talk", [False, True])
async def test_stop_during_non_stream_generation_keeps_usage_not_text(
    monkeypatch, small_talk
):
    faq, _ = service(monkeypatch)
    sink = Sink()
    generated = completion(f"{SMALL_TALK_PREFIX}\nHello." if small_talk else ANSWER)

    async def generate(*args, **kwargs):
        sink.stopped = True
        return generated

    faq.llm_gateway.generate.side_effect = generate
    verdict = Verdict(
        route=Route.SMALL_TALK if small_talk else Route.ANSWER,
        shown=SHOWN,
        decision=DECISION.model_copy(update={"match": "other_edition"}),
    )
    branch = faq._scheme_small_talk if small_talk else faq._scheme_answer
    result = await branch(QUESTION, verdict, NotFoundMode.GENERAL, sink)

    assert result.stopped
    assert result.content == ""
    assert sink.deltas == []
    assert result.completions == [generated]
    faq.llm_gateway.generate.assert_awaited_once()


async def test_filtered_parallel_general_never_emits_content(monkeypatch):
    faq, _ = service(monkeypatch)
    sink = Sink()
    verdict = Verdict(
        route=Route.NOT_FOUND,
        shown=[],
        general=completion(
            f"{GENERAL_ANSWER_PREFIX}\n" + "Filtered provider content. " * 10,
            FinishReason.FILTERED,
        ),
    )
    result = await faq._scheme_not_found(QUESTION, verdict, NotFoundMode.GENERAL, sink)

    assert result.content == REFUSAL_ANSWER
    assert result.origin is AnswerOrigin.NONE
    assert sink.deltas == []
    assert result.completions == []  # The outer decision path adds it once.


async def test_filtered_small_talk_refuses_without_emitting_or_falling_back(
    monkeypatch,
):
    filtered = completion(f"{SMALL_TALK_PREFIX}\nHello.", FinishReason.FILTERED)
    faq, _ = service(monkeypatch, filtered)
    sink = Sink()
    result = await faq._scheme_small_talk(
        QUESTION, Verdict(route=Route.SMALL_TALK, shown=[]), NotFoundMode.GENERAL, sink
    )

    assert result.content == REFUSAL_ANSWER
    assert result.origin is AnswerOrigin.NONE
    assert sink.deltas == []
    assert result.completions == [filtered]
    faq.llm_gateway.generate.assert_awaited_once()


async def test_stream_stop_retains_only_visible_prefix_and_closes_stream(monkeypatch):
    faq, _ = service(monkeypatch)
    sink = Sink(stop_after_delta=True)
    closed = []

    async def stream(*args, **kwargs):
        try:
            yield "First sentence. "
            yield "Unwanted rest."
            yield completion("First sentence. Unwanted rest.")
        finally:
            closed.append(True)

    faq.llm_gateway.stream = stream
    result = await faq._scheme_answer(
        QUESTION, Verdict(route=Route.ANSWER, shown=SHOWN), NotFoundMode.GENERAL, sink
    )

    assert result.stopped
    assert result.content == "First sentence."
    assert sink.deltas == ["First sentence. "]
    assert closed == [True]
    assert len(result.completions) == 1
    assert result.completions[0].finish_reason is FinishReason.TRUNCATED


async def test_external_cancellation_propagates_and_awaits_speculation_cleanup(
    monkeypatch,
):
    faq, _ = service(monkeypatch)
    started = asyncio.Event()
    cleaned = asyncio.Event()
    sink = Sink()

    async def generate(*args, **kwargs):
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    async def decide(*args, **kwargs):
        await started.wait()
        raise asyncio.CancelledError

    faq.llm_gateway.generate.side_effect = generate
    faq.decider.decide.side_effect = decide
    with pytest.raises(asyncio.CancelledError):
        await decided(faq, sink)

    assert cleaned.is_set()
    assert sink.deltas == []


async def test_successful_speculation_has_one_completion_and_keeps_sources(monkeypatch):
    faq, _ = service(monkeypatch)
    sink = Sink()
    result = await decided(faq, sink)

    assert result.content == ANSWER
    assert result.origin is AnswerOrigin.DOCUMENTS
    assert result.sources == SHOWN
    assert not result.stopped
    assert sink.deltas == [ANSWER]
    assert len(result.completions) == 1
