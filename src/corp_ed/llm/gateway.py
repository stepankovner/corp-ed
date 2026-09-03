from abc import ABC, abstractmethod

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
    ) -> Completion: ...
