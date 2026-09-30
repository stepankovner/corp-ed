"""Порядок фрагментов после реранкера (M3, Р-14) — чистые функции.

Правила из замера ML (eval/rerank.py): порог отказа — по расстоянию
вектора, реранкер только выбирает лучшие k из прошедших порог; при
равных баллах сохраняется порядок вектора. Что показывать модели —
embed_text (крошки «Документ > Раздел» и текст, как видит эмбеддер; так
мерил ML) или content (текст для промпта).
"""

from collections.abc import Sequence
from enum import StrEnum

from corp_ed.domain.types import ChunkMatch


class RerankText(StrEnum):
    EMBED = "embed"
    LLM = "llm"


def rerank_passage(match: ChunkMatch, kind: RerankText) -> str:
    if kind is RerankText.EMBED and match.embed_text:
        return match.embed_text
    return match.content


def rerank_order(
    matches: Sequence[ChunkMatch], scores: Sequence[float]
) -> list[ChunkMatch]:
    """Фрагменты по убыванию балла; при равных — в исходном порядке."""
    if len(matches) != len(scores):
        raise ValueError("matches and scores differ in length")
    order = sorted(range(len(matches)), key=lambda i: (-scores[i], i))
    return [matches[i] for i in order]
