"""Замер памяти диалога (ML-2): набор, один ход диалога, сводка."""

from dataclasses import dataclass
from pathlib import Path

import pytest

from corp_ed.llm.types import Message
from corp_ed.prompts.dialogue import Turn
from eval.dialogue_eval import (
    Search,
    TurnCache,
    TurnSettings,
    concat_query,
    found_rank,
    load_dialogues,
    run_turn,
    same_question,
    summarize,
)
from eval.offline_e2e import OfflineMatch

HEADER = (
    "id,history,question,expected_standalone,expected_answer,expected_material,"
    "expected_section,in_corpus,type,kind,level,evidence,author,split\n"
)


@dataclass
class FakeReply:
    text: str
    input_tokens: int = 100
    output_tokens: int = 10
    latency_ms: float = 5.0


def _match(content: str, title: str = "UMNIK-2026") -> OfflineMatch:
    return OfflineMatch(content=content, title=title, distance=0.3)


def test_load_dialogues(tmp_path: Path) -> None:
    path = tmp_path / "d.csv"
    path.write_text(
        HEADER + "d01,Сколько длится проект Старт-ИИ? || А сколько дают?,А в УМНИК?,"
        "Сколько дают в УМНИК?,500 тыс.,UMNIK-2026,,true,fact,followup,деталь,"
        ",llm,dev\n"
        "s01,Вопрос про НДС,Кто оператор?,,ООО,Правила,,true,fact,standalone,"
        "деталь,,llm,dev\n",
        encoding="utf-8",
    )
    first, second = load_dialogues(path)

    assert first.history == ["Сколько длится проект Старт-ИИ?", "А сколько дают?"]
    assert first.item.question == "А в УМНИК?"
    assert first.expected_standalone == "Сколько дают в УМНИК?"
    # Пустой expected_standalone — вопрос и есть самостоятельный.
    assert second.expected_standalone == "Кто оператор?"

    path.write_text(
        HEADER + "x,,Вопрос,,,,,true,fact,followup,,,llm,dev\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="пустая история"):
        load_dialogues(path)


def test_same_question_and_concat() -> None:
    assert same_question("Кто оператор акселератора?", " кто  оператор акселератора ")
    assert same_question("Ещё вопрос?", "Еще вопрос")
    assert not same_question("А в УМНИК?", "Какой грант в УМНИК?")
    assert concat_query(["Первый?", "Второй?"], "А третий?") == "Второй? А третий?"


def test_found_rank_uses_golden_relevance() -> None:
    from eval.datasets import EvalItem

    item = EvalItem(
        id="d01",
        question="А в УМНИК?",
        in_corpus=True,
        type="fact",
        expected_material="UMNIK-2026",
        evidence="от 18 до 35 лет",
    )
    matches = [
        _match("Другое", title="Старт-ИИ"),
        _match("гражданами РФ в возрасте от 18 до 35 лет включительно"),
    ]
    assert found_rank(item, matches) == 2
    assert found_rank(item, matches[:1]) == 0


class Recorder:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list[Message]] = []

    def __call__(self, messages: list[Message]) -> FakeReply:
        self.calls.append(messages)
        return FakeReply(self.replies.pop(0))


def _search_log() -> tuple[list[str], Search]:
    queries: list[str] = []

    def search(query: str) -> tuple[list[OfflineMatch], float | None]:
        queries.append(query)
        return [_match("Грант УМНИК — 500 тыс. рублей")], 0.3

    return queries, search


def test_turn_with_history_condenses_and_shows_history() -> None:
    queries, search = _search_log()
    condense = Recorder(["Какой размер гранта УМНИК?"])
    ask = Recorder(["500 тыс. рублей [1]"])
    history = [Turn("Какой грант в Старт-ИИ-1?", "До 5 млн рублей [1]")]

    result = run_turn(
        "А в УМНИК?",
        history,
        search=search,
        ask=ask,
        ask_condense=condense,
        context=list,
        settings=TurnSettings(max_distance=0.59),
    )

    assert queries == ["Какой размер гранта УМНИК?"]
    assert result.standalone == "Какой размер гранта УМНИК?"
    prompt = "\n".join(m.content for m in ask.calls[0])
    assert "Какой грант в Старт-ИИ-1?" in prompt
    assert result.tokens == 220 and result.condense_tokens == 110
    assert result.calls == 2 and not result.general


def test_turn_without_memory_or_history_skips_condense() -> None:
    queries, search = _search_log()
    condense = Recorder([])
    ask = Recorder(["Ответ [1]", "Ответ [1]"])
    history = [Turn("Прошлый вопрос?", "Прошлый ответ")]
    settings = TurnSettings(max_distance=0.59)

    run_turn(
        "А в УМНИК?",
        history,
        search=search,
        ask=ask,
        ask_condense=condense,
        context=list,
        settings=settings,
        use_memory=False,
    )
    run_turn(
        "Вопрос?",
        [],
        search=search,
        ask=ask,
        ask_condense=condense,
        context=list,
        settings=settings,
    )

    assert condense.calls == []
    assert queries == ["А в УМНИК?", "Вопрос?"]
    assert "Прошлый вопрос?" not in "\n".join(m.content for m in ask.calls[0])


def test_turn_beyond_threshold_gets_general_answer_by_standalone() -> None:
    def search(query: str) -> tuple[list[OfflineMatch], float | None]:
        far = OfflineMatch(content="чужое", title="X", distance=0.8)
        return [far], 0.8

    condense = Recorder(["Какой дресс-код на демо-дне?"])
    ask = Recorder(["Обычно деловой стиль."])
    result = run_turn(
        "А дресс-код?",
        [Turn("Демо-день очный?", "Да")],
        search=search,
        ask=ask,
        ask_condense=condense,
        context=list,
        settings=TurnSettings(max_distance=0.59),
    )

    assert result.general and result.selected == []
    assert result.answer.startswith("В документах компании ответа нет")
    # Общий ответ — по переписанному вопросу: «А дресс-код?» модели не понять.
    assert "Какой дресс-код на демо-дне?" in ask.calls[0][-1].content


def test_turn_cache_key_depends_on_history() -> None:
    first = TurnCache.key("flash", [], "Вопрос?", "d0.59")
    assert first == TurnCache.key("flash", [], "Вопрос?", "d0.59")
    assert first != TurnCache.key("flash", [Turn("q", "a")], "Вопрос?", "d0.59")


def test_summarize_counts_by_kind() -> None:
    rows: list[dict[str, object]] = [
        {
            "kind": "followup",
            "rank_raw": 0,
            "rank_concat": 2,
            "rank_mem": 1,
            "passed_raw": False,
            "passed_mem": True,
            "general_raw": True,
            "general_mem": False,
            "condense_unchanged": False,
            "tokens_mem": 3900,
        },
        {
            "kind": "standalone",
            "rank_raw": 1,
            "rank_concat": 1,
            "rank_mem": 1,
            "passed_raw": True,
            "passed_mem": True,
            "general_raw": False,
            "general_mem": False,
            "condense_unchanged": True,
            "tokens_mem": 4100,
        },
    ]
    text = "\n".join(summarize(rows))

    assert "нужный фрагмент в топ-5, raw: 0 / 1" in text
    assert "нужный фрагмент в топ-5, mem: 1 / 1" in text
    assert "переписывание не изменило вопрос: 1 / 1" in text
    assert "1 — 1, 2+ — 1" in text
