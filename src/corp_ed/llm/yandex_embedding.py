import time
from typing import Any

import httpx

from corp_ed.llm.embedding_gateway import EmbeddingGateway
from corp_ed.llm.errors import LLMError
from corp_ed.llm.retry import call_with_retry
from corp_ed.llm.throttle import Throttle
from corp_ed.llm.types import EmbeddingResult

URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/textEmbedding"
EMBEDDING_DIM = 256


def _parse(body: dict[str, Any], model: str, latency_ms: int) -> EmbeddingResult:
    embedding = [float(x) for x in body["embedding"]]

    if len(embedding) != EMBEDDING_DIM:
        raise LLMError(
            f"unexpected embedding size: {len(embedding)}, expected {EMBEDDING_DIM}",
            retryable=False,
        )

    return EmbeddingResult(
        embedding=embedding,
        model=model,
        model_version=body["modelVersion"],
        latency_ms=latency_ms,
        input_tokens=int(body["numTokens"]),
    )


class YandexEmbeddingAdapter(EmbeddingGateway):
    """Эмбеддинги Yandex Foundation Models.

    Документы и запросы считаются разными моделями одной пары: они дают
    векторы в общем пространстве, но обучены с разных сторон. Перепутать
    их — не ошибка, а тихая потеря качества поиска.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        folder_id: str,
        api_key: str,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        read_timeout: float = 60.0,
        document_throttle: Throttle | None = None,
        query_throttle: Throttle | None = None,
    ):
        self._client = client
        # Квота эмбеддингов — 10 запросов в секунду на каталог, общая для
        # вопросов и ингеста. У каждой стороны свой темп, чтобы воркер не
        # съедал квоту, пока сотрудник ждёт ответа (BH-4).
        self._document_throttle = document_throttle
        self._query_throttle = query_throttle
        self._folder_id = folder_id
        self._api_key = api_key
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._read_timeout = read_timeout

    async def embed_document(self, text: str) -> EmbeddingResult:
        return await self._embed(text, "text-search-doc", self._document_throttle)

    async def embed_query(self, text: str) -> EmbeddingResult:
        return await self._embed(text, "text-search-query", self._query_throttle)

    async def _embed(
        self, text: str, model: str, throttle: Throttle | None
    ) -> EmbeddingResult:
        payload = {"modelUri": f"emb://{self._folder_id}/{model}/latest", "text": text}
        timings: list[int] = []

        async def do_request() -> httpx.Response:
            # Слот берёт КАЖДАЯ попытка, в том числе повтор после 429:
            # иначе повторы «все разом» снова упираются в квоту.
            if throttle is not None:
                await throttle.acquire()
            started = time.perf_counter()
            response = await self._client.post(
                URL,
                json=payload,
                headers={"Authorization": f"Api-Key {self._api_key}"},
                timeout=httpx.Timeout(
                    connect=5.0,
                    read=self._read_timeout,
                    write=10.0,
                    pool=5.0,
                ),
            )

            timings.append(int((time.perf_counter() - started) * 1000))
            return response

        response = await call_with_retry(
            do_request,
            max_attempts=self._max_attempts,
            base_delay=self._base_delay,
        )

        return _parse(response.json(), model=model, latency_ms=timings[-1])
