"""Сборка контекста для LLM: бюджет токенов и small-to-big.

select_context (A4, pre-MVP) — берёт найденные чанки по порядку, пока они
влезают в бюджет. Урок «Честного знака»: полные статьи до 120 000 токенов
в контексте — дорого, медленно, и модель теряет фокус. Стартовый бюджет
3000 токенов — допущение досье 8.2, на нём же считается себестоимость.

select_sections (M2, черновик) — ищем маленькими чанками, в LLM отдаём
родительскую секцию целиком, если она влезает. Дедупликация по секции
делается ПОСЛЕ финального ранжирования: у МТС первый чанк на секцию
брался до реранкера, и если сортировка ошибалась, правильная секция
выбывала навсегда.
"""

from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from corp_ed.domain.tokens import count_tokens


class HasContent(Protocol):
    @property
    def content(self) -> str: ...


class SectionChunk(Protocol):
    """Найденный чанк, у которого известна родительская секция (M2)."""

    @property
    def content(self) -> str: ...

    @property
    def title(self) -> str: ...

    @property
    def heading_path(self) -> Sequence[str]: ...

    @property
    def section_id(self) -> Hashable: ...


def select_context[T: HasContent](matches: Sequence[T], max_tokens: int) -> list[T]:
    """Чанки в исходном порядке, суммарно не больше max_tokens.

    Не влезший чанк пропускается, следующие (они могут быть короче) ещё
    пробуются. На pre-MVP чанки одного размера, и разницы с «остановиться
    на первом не влезшем» нет; для секций разной длины (M2) пропуск
    лучше заполняет бюджет. Считается только текст чанка: обвязка промпта
    (номер, источник) — порядка 20 токенов на выдержку, в запасе бюджета.
    """
    selected: list[T] = []
    budget = max_tokens
    for match in matches:
        cost = count_tokens(match.content)
        if cost <= budget:
            selected.append(match)
            budget -= cost
    return selected


@dataclass(frozen=True)
class ContextBlock[T]:
    """Выдержка для промпта: секция целиком или отдельный чанк.

    title и heading_path — как у найденного чанка, поэтому блок подходит
    для prompts.faq.build_faq_messages. match — чанк, по которому блок
    попал в контекст: его отдавать в источники ответа.
    """

    content: str
    title: str
    heading_path: Sequence[str]
    match: T
    whole_section: bool


def select_sections[T: SectionChunk](
    matches: Sequence[T],
    sections: Mapping[Hashable, str],
    max_tokens: int,
) -> list[ContextBlock[T]]:
    """Small-to-big: для каждого чанка — его секция, если влезает, иначе он сам.

    matches — в финальном порядке (после RRF / реранкера). Секция
    попадает в контекст на месте своего лучшего чанка; остальные чанки
    той же секции пропускаются — они уже внутри. Если секция не влезает
    в остаток бюджета, в контекст идёт сам чанк, и тогда другие чанки
    этой секции ещё могут попасть сами по себе.

    sections — текст секций по section_id (бэкенд достаёт из таблицы
    sections). Секции нет в словаре — берётся чанк.

    Запасной путь, если секции окажутся слишком большими для бюджета:
    вместо секции — чанк и N соседей (так у Битрикс24, N = 5). Решать по
    eval на корпусе.
    """
    blocks: list[ContextBlock[T]] = []
    whole: set[Hashable] = set()
    budget = max_tokens

    for match in matches:
        if match.section_id in whole:
            continue

        section_text = sections.get(match.section_id)
        candidates = [(match.content, False)]
        if section_text is not None:
            candidates.insert(0, (section_text, True))

        for text, is_section in candidates:
            cost = count_tokens(text)
            if cost > budget:
                continue
            blocks.append(
                ContextBlock(
                    content=text,
                    title=match.title,
                    heading_path=match.heading_path,
                    match=match,
                    whole_section=is_section,
                )
            )
            budget -= cost
            if is_section:
                whole.add(match.section_id)
            break

    return blocks
