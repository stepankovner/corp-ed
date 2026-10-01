"""Реранкер (M3, BH-32): балл пары «вопрос — фрагмент».

Вектор находит кандидатов по смыслу, кросс-энкодер читает пару целиком и
ставит балл — порядок точнее. Замер ML (ml-report.md, «M3 вариант б»):
cross-encoder/mmarco-mMiniLMv2-L12-H384-v1 — нужный фрагмент среди пяти
у 93 % вопросов против 75 % у одного вектора, верных ответов 19 → 23 из
27, +1,8 с к ответу на 4 vCPU. Кого и как переставлять — правило ML
domain.rerank.rerank; здесь только баллы.

Модель работает отдельным сервисом (compose.yaml, профиль reranker):
HuggingFace text-embeddings-inference с ручкой POST /rerank. Контракт
BH-32 допускает и модель в процессе API, но тогда образ API тянет torch
(+1 ГБ), а память и процессор модели делятся с ответами; сервис
ограничен отдельно, и модель меняется без пересборки приложения. Любой
сервис с тем же контрактом подходит.

Сбой, таймаут или кривой ответ реранкера — не сбой ответа: FaqService
остаётся с порядком вектора.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence

import httpx

from corp_ed.domain.types import ChunkMatch


def rerank_passage(match: ChunkMatch) -> str:
    """Что видит модель: embed_text — крошки «Документ > Раздел» и текст,
    как видит эмбеддер; на нём замерено качество (BH-32). У чанков до
    крошек embed_text пуст — тогда текст для промпта."""
    return match.embed_text or match.content


class RerankerError(Exception):
    """Реранкер недоступен или ответил не по контракту."""


class Reranker(ABC):
    model: str
    """Имя модели — в журнал ответов и диагностику."""

    @abstractmethod
    async def score(self, query: str, passages: Sequence[str]) -> list[float]:
        """Балл каждого фрагмента, в порядке passages; больше — лучше."""


class HttpReranker(Reranker):
    """Контракт text-embeddings-inference: POST {url}/rerank.

    Запрос {"query", "texts", "truncate"}; ответ — список {"index",
    "score"} в порядке убывания балла. Длинные фрагменты сервис обрезает
    сам (truncate) по окну модели — 512 токенов, как max_length=512 в
    замере ML (RAG_RERANK_MAX_LENGTH).
    """

    def __init__(
        self, client: httpx.AsyncClient, url: str, *, model: str, timeout: float
    ) -> None:
        self._client = client
        self._url = url.rstrip("/") + "/rerank"
        self._timeout = timeout
        self.model = model

    async def score(self, query: str, passages: Sequence[str]) -> list[float]:
        try:
            response = await self._client.post(
                self._url,
                json={"query": query, "texts": list(passages), "truncate": True},
                timeout=httpx.Timeout(self._timeout),
            )
        except httpx.HTTPError as exc:
            raise RerankerError(type(exc).__name__) from exc
        if response.status_code != 200:
            raise RerankerError(f"HTTP {response.status_code}")
        return parse_scores(response, len(passages))


def parse_scores(response: httpx.Response, count: int) -> list[float]:
    try:
        items = response.json()
        scores: dict[int, float] = {int(i["index"]): float(i["score"]) for i in items}
    except (ValueError, TypeError, KeyError) as exc:
        raise RerankerError("malformed body") from exc
    if sorted(scores) != list(range(count)):
        raise RerankerError("scores do not match passages")
    return [scores[index] for index in range(count)]


class FakeReranker(Reranker):
    """Для тестов и разработки: балл — сколько слов вопроса во фрагменте."""

    model = "fake-reranker"

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    async def score(self, query: str, passages: Sequence[str]) -> list[float]:
        self.calls.append((query, list(passages)))
        words = {w for w in query.casefold().split() if len(w) > 3}
        return [
            float(sum(word in passage.casefold() for word in words))
            for passage in passages
        ]
