import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from corp_ed.core.config import EMBEDDING_DIM, LLMSettings
from corp_ed.llm.errors import LLMError
from corp_ed.llm.yandex_embedding import YandexEmbeddingAdapter, _parse


def make_body(embedding: list[float] | None = None) -> dict[str, Any]:
    if embedding is None:
        embedding = [0.1] * EMBEDDING_DIM
    return {"embedding": embedding, "numTokens": "16", "modelVersion": "06.12.2023"}


def test_parse_builds_result_from_body() -> None:
    result = _parse(
        make_body(), model="m@768", latency_ms=123, expected_dim=EMBEDDING_DIM
    )

    assert len(result.embedding) == EMBEDDING_DIM
    assert result.embedding[0] == 0.1
    assert result.input_tokens == 16
    assert result.model_version == "06.12.2023"
    assert result.model == "m@768"
    assert result.latency_ms == 123


def test_parse_fails_on_wrong_dimension() -> None:
    """Вектор чужой размерности не должен попасть ни в базу, ни в поиск."""
    with pytest.raises(LLMError) as exc_info:
        _parse(make_body([0.1] * 256), model="m", latency_ms=1, expected_dim=768)

    assert exc_info.value.retryable is False


@pytest.mark.parametrize(
    "body",
    [{}, {"embedding": None}, {"embedding": ["x"]}, [], None, {"embedding": [1.0]}],
)
def test_parse_malformed_body_is_llm_error(body: Any) -> None:
    with pytest.raises(LLMError):
        _parse(body, model="m", latency_ms=1, expected_dim=EMBEDDING_DIM)


async def test_request_uses_family_kind_and_dim() -> None:
    """-doc и -query — разные модели; dim уходит только для v2 (BH-16)."""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=make_body())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = YandexEmbeddingAdapter(client=client, folder_id="b1g", api_key="k")

    doc = await adapter.embed_document("документ")
    query = await adapter.embed_query("вопрос")

    assert seen[0]["modelUri"] == "emb://b1g/text-embeddings-v2-doc/latest"
    assert seen[1]["modelUri"] == "emb://b1g/text-embeddings-v2-query/latest"
    assert seen[0]["dim"] == str(EMBEDDING_DIM)
    assert doc.model == f"text-embeddings-v2-doc@{EMBEDDING_DIM}"
    assert query.model == f"text-embeddings-v2-query@{EMBEDDING_DIM}"


async def test_text_search_family_sends_no_dim() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=make_body([0.1] * 256))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = YandexEmbeddingAdapter(
        client=client, folder_id="b1g", api_key="k", family="text-search", dim=256
    )

    await adapter.embed_query("вопрос")

    assert "dim" not in seen[0]


def test_settings_reject_dim_that_does_not_match_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YC_FOLDER_ID", "f")
    monkeypatch.setenv("YC_API_KEY", "k")
    monkeypatch.setenv("EMBEDDING_DIM", "256")

    with pytest.raises(ValidationError, match="migration"):
        LLMSettings()  # type: ignore[call-arg]


def test_settings_reject_quota_overcommit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YC_FOLDER_ID", "f")
    monkeypatch.setenv("YC_API_KEY", "k")
    monkeypatch.setenv("EMBEDDING_QUERY_RPS", "5")
    monkeypatch.setenv("EMBEDDING_INGEST_RPS", "6")

    with pytest.raises(ValidationError, match="quota"):
        LLMSettings()  # type: ignore[call-arg]
