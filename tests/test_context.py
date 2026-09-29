from dataclasses import dataclass, field

from corp_ed.domain.context import (
    ContextBlock,
    Section,
    merge_chunks,
    select_context,
    select_sections,
)
from corp_ed.domain.tokens import count_tokens
from corp_ed.prompts.faq import build_faq_messages


@dataclass(frozen=True)
class Chunk:
    content: str
    section_id: str = "s"
    title: str = "Док"
    heading_path: list[str] = field(default_factory=list)


def _chunk(tokens: int, name: str, section_id: str = "s") -> Chunk:
    # count_tokens = ceil(len / 3): 3 символа на токен.
    return Chunk(content=name.ljust(tokens * 3, "."), section_id=section_id)


# --- select_context --------------------------------------------------------------


def test_select_context_empty() -> None:
    assert select_context([], max_tokens=3000) == []


def test_select_context_takes_all_when_they_fit() -> None:
    chunks = [_chunk(100, "a"), _chunk(100, "b")]

    assert select_context(chunks, max_tokens=3000) == chunks


def test_select_context_respects_budget_exactly() -> None:
    chunks = [_chunk(1000, "a"), _chunk(1000, "b"), _chunk(1000, "c"), _chunk(1, "d")]

    selected = select_context(chunks, max_tokens=3000)

    assert selected == chunks[:3]
    assert sum(count_tokens(c.content) for c in selected) == 3000


def test_select_context_skips_too_long_and_keeps_order() -> None:
    chunks = [_chunk(500, "a"), _chunk(2800, "big"), _chunk(400, "c")]

    assert select_context(chunks, max_tokens=1000) == [chunks[0], chunks[2]]


def test_select_context_zero_budget() -> None:
    assert select_context([_chunk(1, "a")], max_tokens=0) == []


def test_select_context_returns_same_objects() -> None:
    chunk = _chunk(10, "a")

    assert select_context([chunk], max_tokens=100)[0] is chunk


# --- select_sections (M2) -------------------------------------------------------


def test_section_is_taken_whole_once() -> None:
    first = Chunk(content="чанк 1", section_id="s1")
    second = Chunk(content="чанк 2", section_id="s1")

    blocks = select_sections([first, second], {"s1": "вся секция"}, max_tokens=100)

    assert [(b.content, b.whole_section, b.match) for b in blocks] == [
        ("вся секция", True, first)
    ]


def test_section_placed_at_rank_of_its_best_chunk() -> None:
    # Дедупликация после ранжирования: секция s2 идёт первой, потому что её
    # чанк первый в финальной выдаче, хотя s1 встречается чаще.
    ranked = [
        Chunk(content="b1", section_id="s2"),
        Chunk(content="a1", section_id="s1"),
        Chunk(content="a2", section_id="s1"),
    ]

    blocks = select_sections(ranked, {"s1": "секция A", "s2": "секция B"}, 100)

    assert [b.content for b in blocks] == ["секция B", "секция A"]


def test_falls_back_to_chunk_when_section_does_not_fit() -> None:
    first = _chunk(50, "a", section_id="big")
    second = _chunk(50, "b", section_id="big")
    sections = {"big": "x" * 3000}  # 1000 токенов

    blocks = select_sections([first, second], sections, max_tokens=200)

    assert [(b.match, b.whole_section) for b in blocks] == [
        (first, False),
        (second, False),
    ]


def test_chunk_without_known_section() -> None:
    chunk = Chunk(content="сам чанк", section_id="unknown")

    blocks = select_sections([chunk], {}, max_tokens=100)

    assert blocks == [
        ContextBlock(
            content="сам чанк",
            title="Док",
            heading_path=[],
            match=chunk,
            kind="chunk",
        )
    ]


def test_budget_is_respected_across_sections() -> None:
    chunks = [_chunk(10, "a", "s1"), _chunk(10, "b", "s2"), _chunk(10, "c", "s3")]
    sections = {"s1": "x" * 600, "s2": "y" * 600, "s3": "z" * 600}  # по 200 токенов

    blocks = select_sections(chunks, sections, max_tokens=420)

    assert [b.whole_section for b in blocks] == [True, True, False]
    assert sum(count_tokens(b.content) for b in blocks) <= 420


def test_context_blocks_fit_the_prompt() -> None:
    chunk = Chunk(
        content="чанк", section_id="s1", title="Положение", heading_path=["3.2"]
    )

    blocks = select_sections([chunk], {"s1": "Положение > 3.2\nВся секция."}, 100)
    user = build_faq_messages("Вопрос?", blocks)[1].content

    assert "[1] Положение > 3.2\nВся секция." in user


# --- окно соседей и склейка чанков (M2) -----------------------------------------


def _section_chunks() -> list[str]:
    # Как из split_sections: общая строка-крошки, перекрытие — целые
    # предложения с конца предыдущего чанка в начале следующего.
    return [
        "Док > 3\nПервое предложение. Второе предложение.",
        "Док > 3\nВторое предложение.\n\nТретье предложение. Четвёртое.",
        "Док > 3\nЧетвёртое.\nПятое предложение.",
    ]


def test_merge_chunks_keeps_crumbs_once_and_drops_overlap() -> None:
    assert merge_chunks(_section_chunks()) == (
        "Док > 3\nПервое предложение. Второе предложение.\n\n"
        "Третье предложение. Четвёртое.\nПятое предложение."
    )


def test_merge_chunks_without_overlap_joins_with_newline() -> None:
    assert merge_chunks(["Док\nАбзац один.", "Док\nАбзац два."]) == (
        "Док\nАбзац один.\nАбзац два."
    )
    # Первая строка разная — это не крошки, ничего не убирается.
    assert merge_chunks(["первый чанк", "второй чанк"]) == "первый чанк\nвторой чанк"
    assert merge_chunks(["один"]) == "один"
    assert merge_chunks([]) == ""


def test_window_when_section_does_not_fit() -> None:
    texts = _section_chunks()
    section = Section(content="x" * 3000, chunks=texts)  # 1000 токенов
    match = Chunk(content=texts[1])

    blocks = select_sections([match], {"s": section}, max_tokens=100, neighbours=1)

    assert [(b.kind, b.whole_section) for b in blocks] == [("window", False)]
    assert blocks[0].content == merge_chunks(texts)
    assert blocks[0].match is match


def test_window_shrinks_to_fit_and_covers_neighbours() -> None:
    texts = [f"Док\n{letter * 30}" for letter in "abcde"]  # по 12 токенов
    section = Section(content=None, chunks=texts)
    ranked = [Chunk(content=texts[2]), Chunk(content=texts[3]), Chunk(content=texts[0])]

    # ±2 (53 токена) не влезает, ±1 (32) — да; texts[3] уже внутри окна,
    # texts[0] сам по себе (12) в остаток 8 не влезает.
    blocks = select_sections(ranked, {"s": section}, max_tokens=40, neighbours=2)

    assert [(b.kind, b.match) for b in blocks] == [("window", ranked[0])]
    assert blocks[0].content == merge_chunks(texts[1:4])


def test_no_sections_table_and_no_neighbours_is_plain_chunk() -> None:
    chunk = Chunk(content="Док\nтекст")
    section = Section(content=None, chunks=["Док\nтекст"])

    blocks = select_sections([chunk], {"s": section}, max_tokens=100)

    assert [b.kind for b in blocks] == ["chunk"]


def test_same_chunk_twice_is_taken_once() -> None:
    chunk = Chunk(content="Док\nтекст")
    section = Section(chunks=["Док\nтекст"])

    assert len(select_sections([chunk, chunk], {"s": section}, max_tokens=100)) == 1


def test_section_text_as_plain_string_still_works() -> None:
    chunk = Chunk(content="чанк")

    blocks = select_sections([chunk], {"s": "секция"}, max_tokens=100, neighbours=1)

    assert [(b.content, b.kind) for b in blocks] == [("секция", "section")]
