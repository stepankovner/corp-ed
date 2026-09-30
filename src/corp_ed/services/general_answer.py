"""Ответ, когда в документах компании ответа нет: отказ или общий ответ.

Решения команды 28.09 (DECISIONS.md, «Ответ, когда в документах ответа
нет») с поправкой Артёма 29.09 (BH-29):
- новая компания — общий ответ с пометкой; строгий режим (честный
  отказ) включает команда через `cli set-not-found-mode` (NotFoundMode);
- общий ответ — из знаний модели; поиск в интернете — после MVP,
  отдельной реализацией GeneralAnswerSource без переделки FaqService;
- у общего ответа пометка «не из документов компании» в начале и совет
  уточнить у руководителя или в профильном отделе в конце — от кода, а
  не от модели: ни один источник не может их потерять. Тот же совет —
  в тексте отказа (REFUSAL_ANSWER): бот или другой клиент API получит
  его без своей логики.
"""

from typing import Protocol

from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.types import Completion
from corp_ed.prompts.faq import (
    NOT_FOUND_ANSWER,
    build_general_messages,
    ensure_general_prefix,
)

CLARIFY_ADVICE = "Уточните у руководителя или в профильном отделе."
"""Последний абзац любого ответа не из документов (решение 28.09, Q3);
та же фраза — в интерфейсе у отказа (frontend/src/chat/AnswerView.tsx)."""

REFUSAL_ANSWER = f"{NOT_FOUND_ANSWER}\n\n{CLARIFY_ADVICE}"
"""Честный отказ. Начинается с NOT_FOUND_ANSWER: фронт, eval ML
(is_not_found) и журнал распознают отказ по началу текста и полю origin."""


class GeneralAnswerSource(Protocol):
    """Откуда берётся текст общего ответа.

    Сейчас одна реализация — знания модели (ModelKnowledgeSource). Поиск
    в интернете — вторая с тем же методом: найти страницы, передать
    модели выдержки, вернуть её ответ. Пометку и совет ставит
    finalize_general_answer поверх любого источника; фильтр содержимого
    провайдера (FinishReason.FILTERED) разбирает FaqService.
    """

    name: str
    """Для журнала: какой источник ответил."""

    async def generate(self, question: str) -> Completion:
        """Ответ на вопрос сотрудника без документов компании."""
        ...


class ModelKnowledgeSource:
    """Общий ответ из знаний модели — промпт ML без выдержек (Р1)."""

    name = "model"

    def __init__(self, llm_gateway: LLMGateway, *, temperature: float) -> None:
        self.llm_gateway = llm_gateway
        self.temperature = temperature

    async def generate(self, question: str) -> Completion:
        return await self.llm_gateway.generate(
            messages=build_general_messages(question),
            temperature=self.temperature,
        )


def finalize_general_answer(text: str) -> str:
    """Каноническая пометка в начале и совет в конце.

    Совет не дублируется, если текст уже им заканчивается.
    """
    marked = ensure_general_prefix(text)
    if marked.endswith(CLARIFY_ADVICE):
        return marked
    return f"{marked}\n\n{CLARIFY_ADVICE}"
