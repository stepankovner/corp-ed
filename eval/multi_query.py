"""Переформулировки вопросов для стенда (M6): вызов модели с кэшем.

Кэш — JSON {ключ: [переформулировки]}, ключ = версия промпта | модель |
count | вопрос. Повторный прогон бесплатный и воспроизводимый (T = 0).
Стенд ищет по исходному вопросу и по каждой переформулировке, сливает
выдачи domain.query.fuse_query_rankings; в промпт ответа уходит исходный
вопрос.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from corp_ed.prompts.multi_query import (
    DEFAULT_COUNT,
    PROMPT_VERSION,
    QUERIES_SCHEMA,
    build_multi_query_messages,
    parse_queries,
)
from eval.yandex import DEFAULT_API, DEFAULT_LLM, YandexClient

DEFAULT_CACHE = Path("eval/.cache/multi_query.json")


@dataclass(frozen=True)
class Paraphrased:
    queries: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    cached: bool = False
    """Из кэша: вызова не было, токены и время — нули."""


def paraphrase_questions(
    client: YandexClient,
    questions: Sequence[str],
    *,
    count: int = DEFAULT_COUNT,
    model: str = DEFAULT_LLM,
    api: str = DEFAULT_API,
    cache_path: Path = DEFAULT_CACHE,
    max_tokens: int = 400,
) -> list[Paraphrased]:
    """Переформулировки для каждого вопроса (порядок сохраняется)."""
    cache: dict[str, list[str]] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    dirty = False
    results: list[Paraphrased] = []

    for question in questions:
        key = f"{PROMPT_VERSION}|{model}|{count}|{question}"
        if key in cache:
            results.append(Paraphrased(list(cache[key]), cached=True))
            continue
        completion = client.complete(
            build_multi_query_messages(question, count),
            model=model,
            temperature=0.0,
            max_tokens=max_tokens,
            api=api,  # type: ignore[arg-type]
            response_format=QUERIES_SCHEMA,
        )
        queries = parse_queries(completion.text, question=question, count=count)
        cache[key] = queries
        dirty = True
        results.append(
            Paraphrased(
                queries,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                latency_ms=completion.latency_ms,
            )
        )

    if dirty:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8"
        )
    return results
