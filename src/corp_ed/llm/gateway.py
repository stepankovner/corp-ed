from abc import ABC, abstractmethod
from typing import Any

from corp_ed.llm.types import Completion, Message


class LLMGateway(ABC):
    """Контракт вызова языковой модели. Реализации знают про конкретных провайдеров."""

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
