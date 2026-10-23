"""Предохранитель эмбеддингов Яндекса (BH-51).

Эмбеддинги — без резерва: векторы другой модели несовместимы с индексом.
Предохранитель экономит время: N отказов подряд (или ошибка ключа и
оплаты) — open_seconds вопрос сразу получает ошибку, а воркер не берёт
задачи загрузки, вместо того чтобы каждая ждала срок. Свой, отдельный от
предохранителя модели ответа (BH-50): поставщики и квоты разные.

Отказом поставщика не считаются: ThrottleBusyError (наша очередь) и
отказ в самом тексте (400 / 413 / 422 — виноват документ).
"""

from collections.abc import Awaitable, Callable

from corp_ed.llm.embedding_gateway import EmbeddingGateway
from corp_ed.llm.errors import LLMError
from corp_ed.llm.fallback import CircuitBreaker
from corp_ed.llm.throttle import ThrottleBusyError
from corp_ed.llm.types import EmbeddingResult


class BreakerEmbeddings(EmbeddingGateway):
    def __init__(self, inner: EmbeddingGateway, breaker: CircuitBreaker) -> None:
        self.inner = inner
        self.breaker = breaker

    def down(self) -> bool:
        return self.breaker.is_open()

    async def embed_document(self, text: str) -> EmbeddingResult:
        return await self._call(self.inner.embed_document, text)

    async def embed_query(self, text: str) -> EmbeddingResult:
        return await self._call(self.inner.embed_query, text)

    async def _call(
        self, embed: Callable[[str], Awaitable[EmbeddingResult]], text: str
    ) -> EmbeddingResult:
        if not self.breaker.allow_primary():
            raise LLMError("embedding breaker is open", retryable=True)
        try:
            result = await embed(text)
        except ThrottleBusyError:
            raise
        except LLMError as exc:
            if not exc.input_rejected:
                self.breaker.record_failure(exc.kind)
            raise
        self.breaker.record_success()
        return result
