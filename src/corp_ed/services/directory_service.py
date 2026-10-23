"""Справочник «должность → человек» (BH-47) со стороны продукта.

Записи разбирает код ML (domain.directory.parse_directory) из текста
документа оргструктуры. Какой документ — справочник, решает администратор:
при загрузке документ, похожий на оргструктуру (по названию или заголовку
«Организационная структура» в начале, как в eval), получает подсказку
«Похоже на справочник сотрудников: N записей. Использовать?»; без
согласия записи не используются. Записи не хранятся отдельно: на вопрос
они разбираются из текста документа (кэш по тексту) — новая редакция или
синхронизация меняют их сами, удаление и пометка «заменён» убирают.

На вопросе (маршрут DIRECTORY шага решения BH-46): записи, у которых
должность или подразделение покрывают роль (lookup), — одной выдержкой
«Справочник» с источником — документом, из которого они; полное попадание —
все запрошенные поля есть (covers / requested_fields).
"""

import re
from dataclasses import dataclass
from functools import lru_cache
from uuid import NAMESPACE_URL, UUID, uuid5

from corp_ed.domain.directory import (
    DIRECTORY_HEADING,
    Entry,
    covers,
    format_entries,
    lookup,
    parse_directory,
    requested_fields,
)
from corp_ed.domain.types import ChunkMatch
from corp_ed.services.decision_loop import DirectoryHit

_BY_TITLE = re.compile(r"orgstruktura|оргструктур|org[_ -]?chart", re.IGNORECASE)
_BY_HEAD = re.compile(r"организационн\w+\s+структур", re.IGNORECASE)


def looks_like_directory(title: str, text: str) -> bool:
    """Оргструктура — по названию или по заголовку в первых 200 символах
    (правило eval); глоссарий и прочие «справочники» — нет."""
    return bool(_BY_TITLE.search(title) or _BY_HEAD.search(text[:200]))


def directory_hint(title: str, text: str) -> int | None:
    """Сколько записей распознано, если документ похож на оргструктуру и
    записи есть; иначе None — подсказки нет."""
    if not looks_like_directory(title, text):
        return None
    return len(entries_of(text)) or None


@lru_cache(maxsize=64)
def _parsed(text: str) -> tuple[Entry, ...]:
    return tuple(parse_directory(text))


def entries_of(text: str) -> list[Entry]:
    return list(_parsed(text))


@dataclass(frozen=True)
class Book:
    """Справочник, доступный сотруднику: документ и его записи."""

    material_id: UUID
    title: str
    entries: list[Entry]


def find(books: list[Book], role: str, missing: str) -> DirectoryHit:
    """Записи по роли — первый справочник, где они нашлись (BH-47 п. 1–3)."""
    for book in books:
        found = lookup(book.entries, role)
        if not found:
            continue
        content = format_entries(found)
        match = ChunkMatch(
            id=uuid5(NAMESPACE_URL, f"directory:{book.material_id}:{content}"),
            content=content,
            material_id=book.material_id,
            position=0,
            distance=0.0,
            title=book.title,
            heading_path=[DIRECTORY_HEADING],
        )
        return DirectoryHit(
            match=match,
            entries=found,
            complete=covers(found, requested_fields(missing)),
        )
    return DirectoryHit(match=None, entries=[], complete=False)
