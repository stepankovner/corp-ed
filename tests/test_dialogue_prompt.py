"""Память диалога (ML-2): переписывание вопроса и история в промпте."""

from corp_ed.prompts.dialogue import (
    CONDENSE_PROMPT_VERSION,
    Turn,
    build_condense_messages,
    format_history,
    parse_condensed,
    recent_turns,
)
from corp_ed.prompts.faq import build_faq_messages


class _Chunk:
    def __init__(self, content: str) -> None:
        self.content = content
        self.title = "Положение"
        self.heading_path = ["2. Условия"]


TURNS = [
    Turn("Сколько длится проект Старт-ИИ-1?", "12 месяцев с даты договора [1]."),
    Turn("Какой размер гранта Старт-ИИ-1?", "До 5 млн рублей [2]."),
]


def test_recent_turns_keeps_last_in_order() -> None:
    turns = [Turn(f"в{i}", f"о{i}") for i in range(5)]

    assert [t.question for t in recent_turns(turns, 3)] == ["в2", "в3", "в4"]
    assert recent_turns(turns, 0) == []


def test_format_history_strips_citations_and_keeps_newest_within_budget() -> None:
    text = format_history(TURNS)

    assert text.index("Сколько длится") < text.index("Какой размер")
    assert "[1]" not in text and "[2]" not in text
    assert "Ассистент: До 5 млн рублей." in text

    long_old = Turn("Старый вопрос?", "очень длинный ответ " * 200)
    tight = format_history([long_old, TURNS[1]], max_tokens=40)
    assert "Какой размер гранта" in tight
    assert "Старый вопрос" not in tight


def test_long_answers_are_cut() -> None:
    text = format_history([Turn("Вопрос?", "слово " * 500)])

    assert text.endswith("…")
    assert len(text) < 600


def test_condense_messages_have_dialogue_and_question() -> None:
    messages = build_condense_messages(TURNS, "А для УМНИК?")

    assert "не отвечай на вопрос" in messages[0].content.casefold()
    assert "Сотрудник: Какой размер гранта Старт-ИИ-1?" in messages[1].content
    assert messages[1].content.rstrip().endswith("Новый вопрос: А для УМНИК?\nОтвет:")
    assert CONDENSE_PROMPT_VERSION == "condense-v1"


def test_parse_condensed_cleans_and_falls_back() -> None:
    question = "А для УМНИК?"

    assert parse_condensed("Какой размер гранта УМНИК?", question) == (
        "Какой размер гранта УМНИК?"
    )
    assert parse_condensed("Ответ: «Какой размер гранта УМНИК?»\n", question) == (
        "Какой размер гранта УМНИК?"
    )
    assert parse_condensed("", question) == question
    assert parse_condensed("   \n  ", question) == question
    assert parse_condensed("В документах компании ответа нет.", question) == question
    assert parse_condensed("Грант " * 100, question) == question


def test_faq_prompt_without_history_is_unchanged() -> None:
    chunks = [_Chunk("Объём гранта — до 5 млн рублей.")]

    plain = build_faq_messages("Какой грант?", chunks)
    empty = build_faq_messages(
        "Какой грант?", chunks, history=[], standalone_question="x"
    )

    assert [m.content for m in plain] == [m.content for m in empty]
    assert "Начало диалога" not in plain[1].content
    assert "Вопрос сотрудника: Какой грант?\n\n" in plain[1].content


def test_faq_prompt_with_history_shows_dialogue_and_standalone_question() -> None:
    chunks = [_Chunk("Объём гранта УМНИК — 500 тыс. рублей.")]

    messages = build_faq_messages(
        "А для УМНИК?",
        chunks,
        history=TURNS,
        standalone_question="Какой размер гранта УМНИК?",
    )
    user = messages[1].content

    assert user.index("Выдержки из документов") < user.index("Начало диалога")
    assert user.index("Начало диалога") < user.index("Вопрос сотрудника")
    assert "факты бери из выдержек" in user
    assert (
        "Вопрос сотрудника: А для УМНИК? (то есть: Какой размер гранта УМНИК?)" in user
    )
    # Переписанный вопрос совпал с исходным — не дублируем.
    same = build_faq_messages(
        "Какой грант УМНИК?",
        chunks,
        history=TURNS,
        standalone_question="Какой грант УМНИК?",
    )
    assert "(то есть:" not in same[1].content
