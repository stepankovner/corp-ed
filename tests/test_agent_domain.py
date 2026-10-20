"""Политика агента и сторож сущностей (вариант 1, agent-v1, 20.10)."""

import json

from corp_ed.domain.agent import (
    cited_numbers,
    drop_citations,
    has_raw_tool_call,
    missing_entities,
    named_entities,
    parse_search_query,
)


def test_search_query_is_read_from_tool_arguments() -> None:
    # gpt-oss присылает кириллицу экранами — json.loads их раскрывает.
    escaped = json.dumps({"query": "  финансовый   директор ФИО "}, ensure_ascii=True)
    assert parse_search_query(escaped) == "финансовый директор ФИО"
    assert parse_search_query('{"query": ""}') is None
    assert parse_search_query("не json") is None
    assert parse_search_query('{"q": "отпуск"}') is None
    assert parse_search_query('["отпуск"]') is None


def test_tool_call_written_as_text_is_caught() -> None:
    # Так Flash ответил при tool_choice="none" (проверка 20.10).
    leaked = '[TOOL_CALL_START]search_documents\n{"query":"ежегодный отпуск"}'
    assert has_raw_tool_call(leaked)
    assert not has_raw_tool_call("Ежегодный отпуск — 28 дней [1].")


def test_named_entities_are_only_explicit_names() -> None:
    assert named_entities("Какие требования к участникам конкурса «Старт-ИИ-3»?") == [
        "Старт-ИИ-3"
    ]
    assert named_entities("Какой размер гранта по программе УМНИК-2025?") == [
        "УМНИК-2025"
    ]
    assert named_entities("Какие направления у акселератора Спринт 3.0?") == [
        "Спринт 3.0"
    ]
    # Обычный вопрос без названий — сторож молчит.
    assert named_entities("Сколько дней ежегодного отпуска положено?") == []


def test_guard_fires_on_a_neighbor_program_only() -> None:
    excerpts = ["Положение о конкурсе «Старт‑ИИ‑1» (очередь 2): грант до 5 млн рублей."]
    # Спросили про соседнюю программу — её в выдержках нет.
    assert missing_entities("Какой грант в «Старт-ИИ-2»?", excerpts) == ["Старт-ИИ-2"]
    # Та же программа, другой регистр и тире — не срабатывает (g10).
    assert (
        missing_entities("Сколько человек в команде СТАРТ-ИИ-1 (очередь 2)?", excerpts)
        == []
    )
    assert missing_entities("Какой грант в «Старт-ИИ-1» (очередь 2)?", excerpts) == []


def test_cited_numbers_in_order_without_repeats() -> None:
    assert cited_numbers("Срок — 3 дня [2]. Форма — АО-1 [1][2].") == [2, 1]


def test_small_talk_reply_loses_citations() -> None:
    reply = "Здравствуйте! Если будут вопросы — спрашивайте. [1][2]"
    assert drop_citations(reply) == "Здравствуйте! Если будут вопросы — спрашивайте."
