from dataclasses import dataclass, field

from corp_ed.domain.context import ContextBlock, select_context, select_sections
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
            whole_section=False,
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
