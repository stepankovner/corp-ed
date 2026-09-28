"""Общий ответ: пометка и совет от кода, источник — подменяемый (28.09)."""

from corp_ed.domain.models import Tenant, User
from corp_ed.domain.types import AnswerOrigin
from corp_ed.llm.fake import FakeAdapter
from corp_ed.llm.types import Completion, FinishReason, Usage
from corp_ed.prompts.faq import GENERAL_ANSWER_PREFIX, NOT_FOUND_ANSWER, is_not_found
from corp_ed.services.faq_service import FaqService
from corp_ed.services.general_answer import (
    CLARIFY_ADVICE,
    REFUSAL_ANSWER,
    ModelKnowledgeSource,
    finalize_general_answer,
)


def _completion(
    content: str, finish: FinishReason = FinishReason.COMPLETED
) -> Completion:
    return Completion(
        content=content,
        finish_reason=finish,
        usage=Usage(input_tokens=3, output_tokens=5),
        model_version="stub",
        model="stub",
        latency_ms=0,
    )


class StubSource:
    """Другой источник общего ответа — как будущий поиск в интернете."""

    name = "stub"

    def __init__(self, completion: Completion) -> None:
        self.completion = completion
        self.questions: list[str] = []

    async def generate(self, question: str) -> Completion:
        self.questions.append(question)
        return self.completion


def test_advice_is_the_last_paragraph() -> None:
    assert finalize_general_answer("Канберра.") == (
        f"{GENERAL_ANSWER_PREFIX}\nКанберра.\n\n{CLARIFY_ADVICE}"
    )


def test_advice_is_not_repeated() -> None:
    once = finalize_general_answer("Канберра.")
    assert finalize_general_answer(once) == once


def test_empty_model_text_still_gets_mark_and_advice() -> None:
    assert finalize_general_answer("  ") == (
        f"{GENERAL_ANSWER_PREFIX}\n\n{CLARIFY_ADVICE}"
    )


async def test_model_source_sends_the_general_prompt() -> None:
    llm = FakeAdapter(content="Ответ.")
    completion = await ModelKnowledgeSource(llm, temperature=0.0).generate("Вопрос?")

    assert completion.content == "Ответ."
    [messages] = llm.calls
    assert "из общих знаний" in messages[0].content
    assert messages[-1].content == "Вопрос сотрудника: Вопрос?"


async def test_service_uses_the_given_source(
    faq_service: FaqService, tenant_ctx: Tenant, employee: User
) -> None:
    """Источник меняется без правки FaqService: пометка и совет остаются."""
    source = StubSource(_completion("Из другого источника."))
    faq_service.general_source = source

    result = await faq_service.answer("Как настроить VPN?", employee)

    assert source.questions == ["Как настроить VPN?"]
    assert result.origin is AnswerOrigin.GENERAL_KNOWLEDGE
    assert result.content == (
        f"{GENERAL_ANSWER_PREFIX}\nИз другого источника.\n\n{CLARIFY_ADVICE}"
    )
    assert result.sources == []


async def test_filtered_source_answer_is_a_refusal(
    faq_service: FaqService, tenant_ctx: Tenant, employee: User
) -> None:
    faq_service.general_source = StubSource(
        _completion("Я не могу обсуждать…", FinishReason.FILTERED)
    )

    result = await faq_service.answer("Вопрос", employee)

    assert result.content == REFUSAL_ANSWER
    assert result.origin is AnswerOrigin.NONE


def test_refusal_starts_with_the_ml_refusal_and_ends_with_advice() -> None:
    """Фронт, eval ML и журнал узнают отказ по началу текста."""
    assert REFUSAL_ANSWER.startswith(NOT_FOUND_ANSWER)
    assert is_not_found(REFUSAL_ANSWER)
    assert REFUSAL_ANSWER.endswith(CLARIFY_ADVICE)
