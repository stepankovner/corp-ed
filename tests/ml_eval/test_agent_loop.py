"""Цикл агента в eval на подделках (вариант 1, agent-v1)."""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from corp_ed.prompts.agent import (
    AGENT_SYSTEM_PROMPT,
    NO_NEW_EXCERPTS,
    QUERY_NOT_PARSED,
    SEARCH_LIMIT_REACHED,
    SEARCH_TOOL,
    build_agent_messages,
)
from corp_ed.prompts.faq import (
    _RULE_8_END,
    _SYSTEM_PROMPT,
    build_faq_messages,
    format_excerpts,
)
from eval.agent_loop import run_agent
from eval.yandex import Completion, ToolCall


@dataclass
class Block:
    title: str
    content: str
    heading_path: list[str] = field(default_factory=list)


def _completion(text: str = "", calls: Sequence[ToolCall] = ()) -> Completion:
    return Completion(
        text=text,
        input_tokens=100,
        output_tokens=10,
        latency_ms=10.0,
        model="fake",
        tool_calls=tuple(calls),
    )


def _call(query: str, n: int = 1) -> ToolCall:
    return ToolCall(
        id=f"call_{n}",
        name="search_documents",
        arguments=json.dumps({"query": query}, ensure_ascii=True),
    )


class Script:
    """Подделка модели: отдаёт заранее заданные ответы и запоминает вызовы."""

    def __init__(self, *answers: Completion) -> None:
        self.answers = list(answers)
        self.seen: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []

    def __call__(
        self, messages: list[dict[str, Any]], tools: Sequence[dict[str, Any]]
    ) -> Completion:
        self.seen.append(([dict(m) for m in messages], list(tools)))
        return self.answers.pop(0)


INITIAL = [
    Block("Регламент закупок", "Закупку свыше 1 млн согласует финансовый директор.")
]
ORG = Block("Оргструктура", "Финансовый директор — Иванова Анна Петровна.")


def test_agent_prompt_keeps_faq_rules_and_data_out_of_system() -> None:
    messages = build_agent_messages("Кто согласует закупку?", INITIAL)
    # Правила 1–8 ответа по выдержкам — те же, что у faq-v2.7.
    rules_1_8 = _SYSTEM_PROMPT.split(_RULE_8_END)[0] + _RULE_8_END
    assert rules_1_8 in AGENT_SYSTEM_PROMPT
    assert "финансовый директор" not in messages[0]["content"]
    assert format_excerpts(INITIAL) in messages[1]["content"]
    # Выдержки агента оформлены так же, как в основном промпте.
    assert format_excerpts(INITIAL) in build_faq_messages("?", INITIAL)[1].content
    assert SEARCH_TOOL["function"]["parameters"]["additionalProperties"] is False
    empty = build_agent_messages("Привет", [])
    assert "ничего не нашлось" in empty[1]["content"]


def test_extra_search_adds_excerpts_with_continued_numbers() -> None:
    chat = Script(
        _completion(calls=[_call("финансовый директор ФИО")]),
        _completion("Согласует Иванова Анна Петровна [2]."),
    )
    run = run_agent(
        "Кто (ФИО) согласует закупку?",
        INITIAL,
        chat=chat,
        search=lambda query: [INITIAL[0], ORG],
    )

    assert run.text == "Согласует Иванова Анна Петровна [2]."
    assert run.queries == ["финансовый директор ФИО"] and run.added == [1]
    assert [b.content for b in run.shown] == [INITIAL[0].content, ORG.content]
    tool_reply = chat.seen[1][0][-1]
    # Уже показанная выдержка не повторяется, новая — под номером 2.
    assert tool_reply["role"] == "tool" and tool_reply["tool_call_id"] == "call_1"
    assert "[2] Оргструктура" in tool_reply["content"]
    assert "[1]" not in tool_reply["content"]


def test_limit_of_two_searches_with_three_parallel_calls() -> None:
    chat = Script(
        _completion(calls=[_call("а", 1), _call("б", 2), _call("в", 3)]),
        _completion("В документах компании ответа нет."),
    )
    searched: list[str] = []

    def search(query: str) -> list[Block]:
        searched.append(query)
        return []

    run = run_agent("Вопрос из трёх частей?", INITIAL, chat=chat, search=search)

    assert searched == ["а", "б"] and run.refused_by_limit == 1
    replies = [m for m in chat.seen[1][0] if m["role"] == "tool"]
    # Каждый вызов получил ответ, иначе следующий запрос не примут.
    assert [m["tool_call_id"] for m in replies] == ["call_1", "call_2", "call_3"]
    assert [m["content"] for m in replies] == [
        NO_NEW_EXCERPTS,
        NO_NEW_EXCERPTS,
        SEARCH_LIMIT_REACHED,
    ]
    # После лимита — вызов без инструментов: модель обязана ответить.
    assert chat.seen[1][1] == []


def test_unparsed_query_and_tool_call_written_as_text() -> None:
    bad = ToolCall(id="call_1", name="search_documents", arguments="не json")
    chat = Script(
        _completion(calls=[bad]),
        _completion('[TOOL_CALL_START]search_documents\n{"query": "x"}'),
        _completion("Ответ по выдержкам [1]."),
    )
    run = run_agent("Вопрос?", INITIAL, chat=chat, search=lambda q: [])

    assert run.unparsed == 1 and chat.seen[1][0][-1]["content"] == QUERY_NOT_PARSED
    # Вызов текстом — не показываем, последний вызов без инструментов.
    assert run.raw_markup and run.forced_final and chat.seen[2][1] == []
    assert run.text == "Ответ по выдержкам [1]."


def test_small_talk_needs_no_search() -> None:
    chat = Script(_completion("Реплика без вопроса.\nЗдравствуйте!"))
    run = run_agent("Привет", [], chat=chat, search=lambda q: [])

    assert run.text.startswith("Реплика без вопроса.") and run.queries == []
    assert len(run.calls) == 1 and chat.seen[0][1] == [SEARCH_TOOL]
