from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import Any

from corp_ed.llm.types import Completion, Message


class LLMGateway(ABC):
    """Контракт вызова языковой модели. Реализации знают про конкретных провайдеров."""

    model_name: str = "unknown"
    """Модель адаптера — для журнала ответа, остановленного до конца потока:
    провайдер не успел её назвать."""

    @abstractmethod
    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
        response_format: dict[str, Any] | None = None,
    ) -> Completion:
        """Сгенерировать ответ.

        response_format — строгий JSON по схеме в формате OpenAI
        ({"type": "json_schema", "json_schema": {"name", "schema", ...}}),
        как GAP_LABEL_SCHEMA в prompts/gaps.py. Адаптер сам переводит его
        в формат своего провайдера.
        """

    async def stream(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
    ) -> AsyncGenerator[str | Completion, None]:
        """Ответ по мере генерации (ТЗ §6): куски текста, последним — Completion.

        Completion в конце — тот же, что вернул бы generate: весь текст,
        причина остановки, токены. По умолчанию — один кусок через
        generate: провайдер без потоковой выдачи работает и так. Прервать
        поток — выйти из цикла и закрыть генератор (contextlib.aclosing):
        адаптер закроет соединение с провайдером.
        """
        completion = await self.generate(
            messages, temperature=temperature, max_tokens=max_tokens
        )
        if completion.content:
            yield completion.content
        yield completion
