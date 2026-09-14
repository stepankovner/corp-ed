from corp_ed.llm.embedding_gateway import EmbeddingGateway
from corp_ed.llm.types import EmbeddingResult


class FakeEmbeddingAdapter(EmbeddingGateway):
    def __init__(self) -> None:
        self.document_calls: list[str] = []
        self.query_calls: list[str] = []

    async def embed_document(self, text: str) -> EmbeddingResult:

        self.document_calls.append(text)

        return EmbeddingResult(
            embedding=[0.1] * 256,
            input_tokens=0,
            model_version="fake",
            model="text-search-doc",
            latency_ms=0,
        )

    async def embed_query(self, text: str) -> EmbeddingResult:

        self.query_calls.append(text)

        return EmbeddingResult(
            embedding=[0.1] * 256,
            input_tokens=0,
            model_version="fake",
            model="text-search-query",
            latency_ms=0,
        )
