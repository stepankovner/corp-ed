from abc import ABC, abstractmethod

from corp_ed.llm.types import EmbeddingResult


class EmbeddingGateway(ABC):
    @abstractmethod
    async def embed_document(self, text: str) -> EmbeddingResult: ...

    @abstractmethod
    async def embed_query(self, text: str) -> EmbeddingResult: ...
