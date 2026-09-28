"""Общий ответ — когда в документах компании ответа нет.

Решения команды 28.09 (DECISIONS.md, «Ответ, когда в документах ответа
нет»):
- новая компания — в строгом режиме (честный отказ); общий ответ
  включает команда через `cli set-not-found-mode` (NotFoundMode);
- общий ответ — из знаний модели; поиск в интернете — после MVP,
  отдельной реализацией GeneralAnswerSource без переделки FaqService;
- у общего ответа пометка «не из документов компании» в начале и совет
  уточнить у руководителя или в профильном отделе в конце — от кода, а
  не от модели: ни один источник не может их потерять.
"""

from typing import Protocol

from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.types import Completion
from corp_ed.prompts.faq import build_general_messages, ensure_general_prefix

GENERAL_ANSWER_ADVICE = "Уточните у руководителя или в профильном отделе."
"""Последний абзац любого общего ответа (решение 28.09, Q3)."""


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
    if marked.endswith(GENERAL_ANSWER_ADVICE):
        return marked
    return f"{marked}\n\n{GENERAL_ANSWER_ADVICE}"
