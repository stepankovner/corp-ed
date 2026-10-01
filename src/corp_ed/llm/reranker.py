"""Реранкер (M3, Р-14): пересортировка найденных фрагментов.

Вектор находит кандидатов по смыслу, кросс-энкодер читает пару «вопрос —
фрагмент» целиком и ставит балл — порядок точнее. Замер ML 30.09
(ml-report.md, «M3 вариант б»): mmarco-mMiniLMv2-L12-H384 даёт Hit@5
0,93 против 0,75 у одного вектора, +1,8 с к ответу на 4 vCPU.

Модель работает отдельным сервисом (compose.yaml, профиль reranker):
HuggingFace text-embeddings-inference с ручкой POST /rerank. Так API не
тянет torch в образ, память и процессор модели ограничены отдельно, а
модель меняется без пересборки приложения. Любой сервис с тем же
контрактом подходит.

Сбой, таймаут или кривой ответ реранкера — не сбой ответа: FaqService
остаётся с порядком вектора.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from enum import StrEnum

import httpx

from corp_ed.domain.types import ChunkMatch


class RerankText(StrEnum):
    """Что показываем реранкеру: embed — крошки «Документ > Раздел» и
    текст, как видит эмбеддер (так мерил ML); llm — текст для промпта."""

    EMBED = "embed"
    LLM = "llm"


def rerank_passage(match: ChunkMatch, kind: RerankText) -> str:
    if kind is RerankText.EMBED and match.embed_text:
        return match.embed_text
    return match.content


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
    сам (truncate): у модели окно 512 токенов.
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
