"""Цикл агента с инструментом поиска для eval (вариант 1, agent-v1).

Модель и поиск подставляются функциями — цикл тестируется на подделках, а
offline_e2e подключает его к нынешнему поиску, порогам BH-37 и общему
ответу. Политика — corp_ed.domain.agent: не больше 2 доборов (каждый
вызов инструмента, и параллельный тоже), не больше 8 выдержек, после
лимита — вызов без инструментов, модель обязана ответить. Каждому вызову
инструмента — ответ role=tool, иначе следующий запрос не примут.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from corp_ed.domain.agent import (
    MAX_EXCERPTS,
    MAX_EXTRA_SEARCHES,
    has_raw_tool_call,
    parse_search_query,
)
from corp_ed.prompts.agent import (
    QUERY_NOT_PARSED,
    SEARCH_LIMIT_REACHED,
    SEARCH_TOOL,
    SEARCH_TOOL_NAME,
    build_agent_messages,
    format_tool_result,
)
from corp_ed.prompts.faq import SourceChunk
from eval.yandex import Completion

Chat = Callable[[list[dict[str, Any]], Sequence[dict[str, Any]]], Completion]
"""(сообщения, инструменты) → ответ модели; пустые инструменты — вызов без них."""


@dataclass
class AgentRun:
    """Итог цикла: текст ответа и что было по дороге."""

    text: str
    shown: list[Any]
    """Выдержки, которые видела модель, по номерам: начальные и добранные."""
    calls: list[Completion] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    added: list[int] = field(default_factory=list)
    """Сколько новых выдержек принёс каждый запрос."""
    refused_by_limit: int = 0
    unparsed: int = 0
    raw_markup: bool = False
    """Модель написала вызов инструмента текстом — ответ пришлось переспросить."""
    forced_final: bool = False


def _key(block: Any) -> str:
    return str(getattr(block, "content", block))


def run_agent(
    question: str,
    initial: Sequence[SourceChunk],
    *,
    chat: Chat,
    search: Callable[[str], Sequence[SourceChunk]],
    max_extra: int = MAX_EXTRA_SEARCHES,
    max_excerpts: int = MAX_EXCERPTS,
) -> AgentRun:
    shown: list[Any] = list(initial)[:max_excerpts]
    messages = build_agent_messages(question, shown)
    run = AgentRun(text="", shown=shown)
    searches = 0
    # Шагов с инструментами не больше max_extra: каждый — хотя бы один поиск.
    for _ in range(max_extra + 1):
        offer = searches < max_extra and len(shown) < max_excerpts
        completion = chat(messages, [SEARCH_TOOL] if offer else [])
        run.calls.append(completion)
        if not completion.tool_calls:
            if has_raw_tool_call(completion.text):
                run.raw_markup = True
                break
            run.text = completion.text.strip()
            return run
        messages.append(
            {
                "role": "assistant",
                "content": completion.text or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                    for call in completion.tool_calls
                ],
            }
        )
        for call in completion.tool_calls:
            if call.name != SEARCH_TOOL_NAME:
                content = f"Неизвестный инструмент: доступен только {SEARCH_TOOL_NAME}."
            elif searches >= max_extra:
                content = SEARCH_LIMIT_REACHED
                run.refused_by_limit += 1
            else:
                searches += 1
                query = parse_search_query(call.arguments)
                if query is None:
                    content = QUERY_NOT_PARSED
                    run.unparsed += 1
                else:
                    run.queries.append(query)
                    seen = {_key(block) for block in shown}
                    room = max(0, max_excerpts - len(shown))
                    new = [b for b in search(query) if _key(b) not in seen][:room]
                    content = format_tool_result(new, start=len(shown) + 1)
                    shown.extend(new)
                    run.added.append(len(new))
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": content}
            )
    # Лимит исчерпан или вызов пришёл текстом — последний вызов без
    # инструментов: модель отвечает по тому, что видела.
    run.forced_final = True
    completion = chat(messages, [])
    run.calls.append(completion)
    text = completion.text.strip()
    run.text = "" if has_raw_tool_call(text) else text
    return run
