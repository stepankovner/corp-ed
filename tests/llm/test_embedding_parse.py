from typing import Any

import pytest

from corp_ed.llm.errors import LLMError
from corp_ed.llm.yandex_embedding import _parse


def make_body(embedding: list[float] | None = None) -> dict[str, Any]:
    if embedding is None:
        embedding = [0.1] * 256
    return {"embedding": embedding, "numTokens": "16", "modelVersion": "06.12.2023"}


def test_parse_builds_result_from_body() -> None:
    result = _parse(make_body(), model="text-search-doc", latency_ms=123)

    assert len(result.embedding) == 256
    assert result.embedding[0] == 0.1
    assert result.input_tokens == 16
    assert result.model_version == "06.12.2023"
    assert result.model == "text-search-doc"
    assert result.latency_ms == 123


def test_parse_fails_on_wrong_dimension() -> None:
    with pytest.raises(LLMError) as exc_info:
        _parse(make_body([0.1, 0.2, 0.3]), model="text-search-doc", latency_ms=123)

    assert exc_info.value.retryable is False


def test_parse_uses_model_from_argument() -> None:
    doc_result = _parse(make_body(), model="text-search-doc", latency_ms=123)
    query_result = _parse(make_body(), model="text-search-query", latency_ms=123)

    assert doc_result.model == "text-search-doc"
    assert query_result.model == "text-search-query"
