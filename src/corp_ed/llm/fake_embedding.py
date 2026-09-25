from corp_ed.core.config import EMBEDDING_DIM
from corp_ed.llm.embedding_gateway import EmbeddingGateway
from corp_ed.llm.types import EmbeddingResult


class FakeEmbeddingAdapter(EmbeddingGateway):
    """Эмбеддер для тестов: постоянный вектор, запись всех входных текстов.

    Размерность — та же, что у схемы (EMBEDDING_DIM), а не зашитое число:
    иначе после смены модели тесты молча проверяли бы старую размерность
    (ловушка из docs/backend-handoff.md, BH-16).
    """

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.dim = dim
        self.document_calls: list[str] = []
        self.query_calls: list[str] = []

    async def embed_document(self, text: str) -> EmbeddingResult:
        self.document_calls.append(text)
        return self._result("text-embeddings-v2-doc")

    async def embed_query(self, text: str) -> EmbeddingResult:
        self.query_calls.append(text)
        return self._result("text-embeddings-v2-query")

    def _result(self, model: str) -> EmbeddingResult:
        return EmbeddingResult(
            embedding=[0.1] * self.dim,
            input_tokens=0,
            model_version="fake",
            model=f"{model}@{self.dim}",
            latency_ms=0,
        )
