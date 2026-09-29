"""Переформулировки для стенда (M6): вызов модели с кэшем, без сети."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from corp_ed.llm.types import Message
from corp_ed.prompts.multi_query import QUERIES_SCHEMA
from eval.multi_query import paraphrase_questions
from eval.yandex import Completion


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float,
        max_tokens: int = 1000,
        api: str = "native",
        response_format: dict[str, Any] | None = None,
    ) -> Completion:
        self.calls.append({"api": api, "format": response_format, "t": temperature})
        question = messages[-1].content.split("\n", 1)[1]
        return Completion(
            text=f'{{"queries": ["{question} (официально)", "{question} кратко"]}}',
            input_tokens=120,
            output_tokens=30,
            latency_ms=900.0,
            model=model,
        )


def test_paraphrases_are_cached_and_reused(tmp_path: Path) -> None:
    client = _FakeClient()
    cache = tmp_path / "mq.json"
    questions = ["Сколько дней отпуска?", "Как оформить ДМС?"]

    first = paraphrase_questions(client, questions, count=2, cache_path=cache)  # type: ignore[arg-type]

    assert [p.queries for p in first] == [
        ["Сколько дней отпуска? (официально)", "Сколько дней отпуска? кратко"],
        ["Как оформить ДМС? (официально)", "Как оформить ДМС? кратко"],
    ]
    assert [p.cached for p in first] == [False, False]
    assert (first[0].input_tokens, first[0].output_tokens) == (120, 30)
    assert len(client.calls) == 2
    # T = 0, строгая схема, тот же API, что у ответов (openai по умолчанию).
    assert client.calls[0] == {"api": "openai", "format": QUERIES_SCHEMA, "t": 0.0}
    assert cache.exists()

    second = paraphrase_questions(client, questions, count=2, cache_path=cache)  # type: ignore[arg-type]

    assert len(client.calls) == 2
    assert [p.cached for p in second] == [True, True]
    assert [p.queries for p in second] == [p.queries for p in first]
    assert second[0].input_tokens == 0


def test_cache_key_includes_count_and_model(tmp_path: Path) -> None:
    client = _FakeClient()
    cache = tmp_path / "mq.json"

    paraphrase_questions(client, ["Вопрос?"], count=2, cache_path=cache)  # type: ignore[arg-type]
    paraphrase_questions(client, ["Вопрос?"], count=1, cache_path=cache)  # type: ignore[arg-type]
    paraphrase_questions(client, ["Вопрос?"], count=1, model="other", cache_path=cache)  # type: ignore[arg-type]

    assert len(client.calls) == 3
