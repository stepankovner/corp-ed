"""Сборка контекста для LLM: бюджет токенов и small-to-big.

select_context (A4, pre-MVP) — берёт найденные чанки по порядку, пока они
влезают в бюджет. Урок «Честного знака»: полные статьи до 120 000 токенов
в контексте — дорого, медленно, и модель теряет фокус. Стартовый бюджет
3000 токенов — допущение досье 8.2, на нём же считается себестоимость.

select_sections (M2) — ищем маленькими чанками, в LLM отдаём больше:
секцию целиком, если она влезает в бюджет; иначе найденный чанк с
соседями по секции (окно, как у Битрикс24); иначе сам чанк. Дедупликация
по секции делается ПОСЛЕ финального ранжирования: у МТС первый чанк на
секцию брался до реранкера, и если сортировка ошибалась, правильная
секция выбывала навсегда.

Почему окно, а не только секция целиком: в демо-корпусе (400/50, замер
25.09) 8 из 175 секций длиннее 3000 токенов — до 14 000 у перечня
приоритетных направлений, — а медиана секции 390 токенов: 93 секции из
175 — это один чанк, и для них small-to-big ничего не меняет.
"""

from collections.abc import Hashable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from corp_ed.domain.tokens import count_tokens

ContextKind = Literal["chunk", "window", "section"]

MIN_OVERLAP_CHARS = 8
"""Короче — случайное совпадение конца одного чанка с началом другого
(«…года.» / «года. Далее…»), а не перекрытие нарезки."""


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
class Section:
    """Что бэкенд знает о секции найденного чанка (M2, BH-13).

    content — llm_text секции целиком (таблица sections); None — таблицы
    нет, и в контекст идёт только окно соседей или сам чанк.
    chunks — llm_text чанков секции по position, для окна соседей; пустой
    список — окна нет. Найденный чанк ищется в списке по тексту, поэтому
    сюда передаётся тот же content, что у найденных чанков.
    """

    content: str | None = None
    chunks: Sequence[str] = ()


@dataclass(frozen=True)
class ContextBlock[T]:
    """Выдержка для промпта: секция целиком, окно соседей или сам чанк.

    title и heading_path — как у найденного чанка, поэтому блок подходит
    для prompts.faq.build_faq_messages. match — чанк, по которому блок
    попал в контекст: его отдавать в источники ответа.
    """

    content: str
    title: str
    heading_path: Sequence[str]
    match: T
    kind: ContextKind

    @property
    def whole_section(self) -> bool:
        return self.kind == "section"


def merge_chunks(texts: Sequence[str]) -> str:
    """Соседние чанки одной секции → один текст без повторов.

    Крошки — первая строка, у чанков одной секции она общая — остаются
    один раз. Перекрытие нарезки (начало следующего чанка дословно
    повторяет конец предыдущего) убирается: ищется самый длинный такой
    повтор. Повтора нет — нарезка без перекрытия или строка таблицы, у
    которой хвост в перекрытие не берётся, — чанки идут через перенос
    строки.
    """
    if not texts:
        return ""
    heads = [text.partition("\n") for text in texts]
    shared = (
        len(texts) > 1
        and all(separator for _, separator, _ in heads)
        and len({crumbs for crumbs, _, _ in heads}) == 1
    )
    crumbs = heads[0][0] if shared else ""
    bodies = [body for _, _, body in heads] if shared else list(texts)

    merged = bodies[0]
    for body in bodies[1:]:
        overlap = _overlap_length(merged, body)
        merged = merged + body[overlap:] if overlap else f"{merged}\n{body}"
    return f"{crumbs}\n{merged}" if crumbs else merged


def _overlap_length(previous: str, current: str) -> int:
    """Длина самого длинного начала current, которым кончается previous."""
    for size in range(min(len(previous), len(current)), MIN_OVERLAP_CHARS - 1, -1):
        if previous.endswith(current[:size]):
            return size
    return 0


def select_sections[T: SectionChunk](
    matches: Sequence[T],
    sections: Mapping[Any, Section | str],
    max_tokens: int,
    *,
    neighbours: int = 0,
) -> list[ContextBlock[T]]:
    """Small-to-big: для каждого чанка — секция, окно соседей или он сам.

    matches — в финальном порядке (после RRF / реранкера). Кандидаты на
    место чанка, по убыванию: секция целиком (если известна и влезает в
    остаток бюджета); окно «чанк ± neighbours» по секции (neighbours,
    neighbours − 1, … 1 — первое, что влезает); сам чанк. Секция или окно
    встают на место своего лучшего чанка; чанки той же секции, которые
    уже в контексте, пропускаются. Не влезло ничего — чанк пропускается,
    следующие ещё пробуются (как select_context).

    sections — по section_id: Section или просто текст секции (то же, что
    Section(content=text)). Секции нет в словаре — берётся сам чанк.
    """
    blocks: list[ContextBlock[T]] = []
    whole: set[Hashable] = set()
    taken: dict[Hashable, set[int]] = {}
    budget = max_tokens

    for match in matches:
        if match.section_id in whole:
            continue
        section = sections.get(match.section_id)
        if isinstance(section, str):
            section = Section(content=section)
        index = _index_in_section(section, match.content)
        if index is not None and index in taken.get(match.section_id, ()):
            continue

        for text, kind, span in _candidates(match, section, index, neighbours):
            cost = count_tokens(text)
            if cost > budget:
                continue
            blocks.append(
                ContextBlock(
                    content=text,
                    title=match.title,
                    heading_path=match.heading_path,
                    match=match,
                    kind=kind,
                )
            )
            budget -= cost
            if kind == "section":
                whole.add(match.section_id)
            elif span is not None:
                taken.setdefault(match.section_id, set()).update(span)
            break

    return blocks


def _index_in_section(section: Section | None, content: str) -> int | None:
    if section is None:
        return None
    try:
        return list(section.chunks).index(content)
    except ValueError:
        return None


def _candidates(
    match: SectionChunk, section: Section | None, index: int | None, neighbours: int
) -> Iterator[tuple[str, ContextKind, range | None]]:
    if section is not None and section.content is not None:
        yield section.content, "section", None
    if section is not None and index is not None:
        for radius in range(neighbours, 0, -1):
            low = max(0, index - radius)
            high = min(len(section.chunks), index + radius + 1)
            if high - low > 1:
                yield merge_chunks(section.chunks[low:high]), "window", range(low, high)
    yield match.content, "chunk", None if index is None else range(index, index + 1)
