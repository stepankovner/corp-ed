from abc import ABC, abstractmethod

from corp_ed.llm.types import EmbeddingResult


class EmbeddingGateway(ABC):
    @abstractmethod
    async def embed_document(self, text: str) -> EmbeddingResult: ...

    @abstractmethod
    async def embed_query(self, text: str) -> EmbeddingResult: ...

    def down(self) -> bool:
        """Поставщик лежит (предохранитель открыт, BH-51): вопрос — сразу
        ошибка, воркер задачи загрузки не берёт."""
        return False
