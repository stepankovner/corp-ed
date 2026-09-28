import math
import re
import zlib

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


class WordEmbeddingAdapter(EmbeddingGateway):
    """Эмбеддер для разработки (LLM_PROVIDER=fake) и сквозных тестов:
    «мешок слов». Общие слова сближают тексты, без общих слов расстояние
    близко к 1 — вопрос по документу находит его, вопрос вне документов
    уходит в общий ответ."""

    async def embed_document(self, text: str) -> EmbeddingResult:
        return self._result(text)

    async def embed_query(self, text: str) -> EmbeddingResult:
        return self._result(text)

    def _result(self, text: str) -> EmbeddingResult:
        vector = [0.0] * EMBEDDING_DIM
        for word in re.findall(r"\w+", text.lower()):
            vector[zlib.crc32(word.encode()) % EMBEDDING_DIM] += 1.0
        norm = math.sqrt(sum(x * x for x in vector)) or 1.0
        return EmbeddingResult(
            embedding=[x / norm for x in vector],
            input_tokens=len(text) // 3,
            model_version="words",
            model=f"words@{EMBEDDING_DIM}",
            latency_ms=0,
        )
