"""Какой найденный чанк считать правильным ответом на вопрос набора.

Проблема, которой нет в ТЗ. ТЗ задаёт эталон серебряного набора как
идентификатор чанка (material, position). Но эксперименты E1–E3 меняют
нарезку, и после переингеста позиции чанков другие: вопрос, сгенерированный
по чанку №12 нарезки 400/50, в нарезке 250/0 отвечается чанком №19.
Эталон по позиции делает сравнение нарезок бессмысленным.

Решение: эталон — не позиция, а ЦИТАТА (evidence) — одно предложение из
чанка, которое содержит ответ; generate_silver.py сохраняет его вместе
с вопросом. Найденный чанк правильный, если содержит цитату. Это не
зависит от нарезки. Для золотого набора правильный чанк — из нужного
документа и нужного раздела (expected_material, expected_section).

Чтобы метрики из eval/metrics.py (один gold-id на вопрос) работали без
изменений, id всех правильных чанков заменяются на один ключ
gold_key(item) — см. canonical_ranking.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from eval.datasets import EvalItem

EVIDENCE_MIN_COVERAGE = 0.8
"""Доля пар соседних слов цитаты, которые должны найтись в чанке.

1.0 — цитата целиком внутри. Запас на случай, когда модель при
генерации чуть изменила пунктуацию или цитата легла на границу чанков
(перекрытие покрывает её только частично)."""

_WORD = re.compile(r"\w+", re.UNICODE)
_FILE_EXTENSION = re.compile(r"\.(?:docx?|pdf|txt|md|markdown|rtf|odt)$", re.IGNORECASE)


@dataclass(frozen=True)
class RetrievedChunk:
    """Чанк из выдачи поиска: /faq/search или офлайн-стенда."""

    id: str
    material: str
    content: str
    heading_path: list[str] = field(default_factory=list)
    distance: float | None = None


def words(text: str) -> list[str]:
    return [word.replace("ё", "е") for word in _WORD.findall(text.casefold())]


def normalize_material(name: str) -> str:
    """«Положение об отпусках.docx» и «положение об отпусках» — один документ."""
    return " ".join(_FILE_EXTENSION.sub("", name.strip()).casefold().split())


def evidence_coverage(evidence: str, content: str) -> float:
    """Доля пар соседних слов цитаты, которые есть в тексте чанка."""
    evidence_words = words(evidence)
    content_words = words(content)
    if not evidence_words:
        return 0.0
    if len(evidence_words) == 1:
        return 1.0 if evidence_words[0] in content_words else 0.0
    evidence_pairs = list(zip(evidence_words, evidence_words[1:], strict=False))
    content_pairs = set(zip(content_words, content_words[1:], strict=False))
    found = sum(1 for pair in evidence_pairs if pair in content_pairs)
    return found / len(evidence_pairs)


def section_matches(expected_section: str, chunk: RetrievedChunk) -> bool:
    """Раздел «3.1» совпадает с заголовком «3.1 Продолжительность» / «3.1. …».

    Сначала по heading_path. Если его нет (нарезка v1 без заголовков) —
    по строке текста чанка, которая начинается с номера раздела.
    """
    expected = " ".join(words(expected_section))
    if not expected:
        return True
    for heading in chunk.heading_path:
        normalized = " ".join(words(heading))
        if normalized == expected or normalized.startswith(expected + " "):
            return True
    return any(
        " ".join(words(line)).startswith(expected + " ")
        for line in chunk.content.split("\n")
        if line.strip()
    )


def is_relevant(item: EvalItem, chunk: RetrievedChunk) -> bool:
    if not item.in_corpus:
        return False
    if item.expected_material and normalize_material(item.expected_material) != (
        normalize_material(chunk.material)
    ):
        return False
    if item.evidence:
        return evidence_coverage(item.evidence, chunk.content) >= EVIDENCE_MIN_COVERAGE
    return section_matches(item.expected_section, chunk)


def gold_key(item: EvalItem) -> str:
    return f"gold:{item.id}"


def canonical_ranking(item: EvalItem, chunks: Sequence[RetrievedChunk]) -> list[str]:
    """Выдача, где id правильных чанков заменены на gold_key(item)."""
    return [
        gold_key(item) if is_relevant(item, chunk) else chunk.id for chunk in chunks
    ]
