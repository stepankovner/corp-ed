"""Контекст документа в каждом чанке (план агента §9.3 п. 4).

Провалы, которые не лечатся промптом (`ml-report.md`, попытка 3): вопрос
про трек «Финтех. Масштабирование» получает выдержки трека «Финтех.
Стартапы» той же программы, и ни поиск, ни модель не видят, что трек
другой — название трека есть только в заголовке документа; вопрос о
редакции 2025 получает выдержки редакции 2026 без отметки года. Практика
(Anthropic Contextual Retrieval, m2tech, МТС — обзор §9.1): в каждый чанк
— короткий контекст документа, редакция — отдельным признаком.

Здесь — чистые функции, без сети:
- `DocContext` — карточка документа (программа, трек, год, редакция),
  которую модель заполняет один раз на документ по его началу строгим JSON
  (схему для запроса строит `prompts/doc_context.py` из этой модели);
- `context_phrase` — одна строка контекста из карточки и статуса редакции;
- `edition_statuses` — какая редакция действующая, какая заменена: по
  корпусу целиком (`domain/editions.py`, правило подсказки BH-41) — по одному
  документу модель этого не знает;
- `insert_context` — строка контекста второй строкой чанка, после крошек:
  первая строка-крошки нужна `format_excerpts` (срезает её в выдержке) и
  нарезке без крошек в замерах E2.
"""

from collections.abc import Hashable, Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from corp_ed.domain.editions import superseded_candidates
from corp_ed.domain.tokens import CHARS_PER_TOKEN

NONE_MARK = "—"
HEAD_TOKENS = 1500
"""Сколько токенов начала документа видит модель: название, шапка, первые
разделы — там программа, трек, год и редакция."""
MAX_FIELD_CHARS = 150
"""Поле длиннее — обрезается: контекст — 50–100 токенов, не пересказ."""
CONTEXT_PREFIX = "О документе: "

EditionStatus = Literal["current", "superseded"]

_STATUS_TEXT: dict[EditionStatus, str] = {
    "current": "действующая редакция",
    "superseded": "прежняя редакция, заменена новой",
}


class DocContext(BaseModel):
    """Карточка документа. Порядок полей — порядок генерации."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(
        description="полное название документа по-русски, как в его тексте"
    )
    organization: str = Field(
        description="организация, которая издала документ или проводит программу, "
        "или «—»"
    )
    program: str = Field(
        description="программа, конкурс, акселератор или проект, к которому "
        "относится документ, или «—»"
    )
    track: str = Field(
        description="трек, направление, очередь или номинация внутри программы, "
        "если документ относится только к нему, или «—»"
    )
    year: str = Field(
        description="год или период, к которому относится документ, или «—»"
    )
    edition: str = Field(
        description="обозначение редакции или версии документа, если указано, или «—»"
    )


def parse_doc_context(text: str) -> DocContext | None:
    """Карточка из ответа модели или None, если схема не соблюдена."""
    try:
        return DocContext.model_validate_json(text)
    except ValidationError:
        return None


def document_head(markdown: str, max_tokens: int = HEAD_TOKENS) -> str:
    """Начало документа не длиннее max_tokens (по оценке domain/tokens).

    Режем по последнему переводу строки или пробелу, чтобы не рвать слово.
    """
    limit = int(max_tokens * CHARS_PER_TOKEN)
    text = markdown.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for separator in ("\n", " "):
        position = cut.rfind(separator)
        if position > limit // 2:
            return cut[:position].rstrip()
    return cut


def _value(raw: str) -> str:
    value = " ".join(raw.split()).strip(" .;")
    if (
        not value
        or value in (NONE_MARK, "-", "–")
        or value.casefold()
        in (
            "нет",
            "не указано",
            "неизвестно",
        )
    ):
        return ""
    if len(value) > MAX_FIELD_CHARS:
        value = value[:MAX_FIELD_CHARS].rsplit(" ", 1)[0] + "…"
    return value


def context_phrase(context: DocContext, status: EditionStatus | None = None) -> str:
    """«О документе: «Название»; программа: …; трек: …; год: …; редакция: …;
    действующая редакция.» — одна строка.

    Пустые и «—» поля пропускаются; поле, которое уже целиком есть в
    названии (год в «Правилах … 2026»), не повторяется. Статус — только
    если у документа есть другая редакция в базе (edition_statuses):
    «действующая» у каждого документа ничего не различает.
    """
    title = _value(context.title)
    parts: list[str] = [f"«{title}»"] if title else []
    seen = title.casefold()
    for label, raw in (
        ("организация", context.organization),
        ("программа", context.program),
        ("трек или направление", context.track),
        ("год", context.year),
        ("редакция", context.edition),
    ):
        value = _value(raw)
        if not value or value.casefold() in seen:
            continue
        parts.append(f"{label}: {value}")
        seen += " " + value.casefold()
    if status is not None:
        parts.append(_STATUS_TEXT[status])
    if not parts:
        return ""
    return CONTEXT_PREFIX + "; ".join(parts) + "."


def edition_statuses[T: Hashable](
    documents: Mapping[T, tuple[str, str]],
) -> dict[T, EditionStatus]:
    """Статус редакции у документов, у которых в базе есть другая редакция.

    documents — id → (название, текст). Правило — подсказка BH-41
    (`superseded_candidates`): название совпадает с точностью до года и
    слов «редакция», «версия», год новее или в тексте сказано, что прежняя
    редакция утрачивает силу. В продукте статус — пометка администратора
    «заменён» (BH-41), здесь — то же правило без подтверждения. Документов
    без пары в ответе нет.
    """
    statuses: dict[T, EditionStatus] = {}
    for document, (title, text) in documents.items():
        others = {
            other: other_title
            for other, (other_title, _) in documents.items()
            if other != document
        }
        replaced = superseded_candidates(title, text, others)
        if replaced:
            statuses.setdefault(document, "current")
        for old in replaced:
            statuses[old] = "superseded"
    return statuses


def insert_context(text: str, crumbs: str, phrase: str) -> str:
    """Строка контекста второй строкой — после крошек, перед текстом.

    Если текст не начинается с крошек (нарезка без крошек, v1) — первой
    строкой. Пустая фраза — текст без изменений.
    """
    if not phrase:
        return text
    prefix = f"{crumbs}\n"
    if crumbs and text.startswith(prefix):
        return f"{prefix}{phrase}\n{text[len(prefix) :]}"
    if crumbs and text == crumbs:
        return f"{crumbs}\n{phrase}"
    return f"{phrase}\n{text}"
