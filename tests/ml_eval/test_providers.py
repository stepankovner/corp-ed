"""Другие поставщики моделей в eval (план резерва 15.10) — без сети и без весов."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from corp_ed.llm.types import Message, Role
from eval.providers import (
    LocalEmbedder,
    OpenAICompatibleClient,
    ProviderError,
    embedding_client,
)
from eval.yandex import EmbeddingCache, YandexClient, embed_many


def _client(handler: httpx.MockTransport) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        "mws",
        "https://api.example/v1/",
        "key",
        http=httpx.Client(transport=handler),
        sleep=lambda _: None,
    )


def test_chat_goes_to_the_provider_with_bearer_key() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.example/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer key"
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": " Ответ [1]. "}, "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": 2189,
                    "completion_tokens": 307,
                    "completion_tokens_details": {"reasoning_tokens": 120},
                },
            },
        )

    completion = _client(httpx.MockTransport(handler)).complete(
        [Message(role=Role.USER, content="вопрос")],
        model="gpt-oss-120b",
        temperature=0,
        extra={"reasoning_effort": "low"},
    )

    # Имя модели — как у поставщика, без gpt:// и каталога Яндекса.
    assert seen[0]["model"] == "gpt-oss-120b"
    assert seen[0]["reasoning_effort"] == "low"
    assert completion.text == "Ответ [1]."
    assert (completion.input_tokens, completion.output_tokens) == (2189, 307)
    assert completion.reasoning_tokens == 120
    assert completion.finish_reason == "stop"


def test_provider_embeddings_and_retries() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="slow down")
        assert str(request.url) == "https://api.example/v1/embeddings"
        assert json.loads(request.content) == {"model": "bge-m3", "input": "текст"}
        return httpx.Response(
            200,
            json={"data": [{"embedding": [0.6, 0.8]}], "usage": {"prompt_tokens": 3}},
        )

    client = _client(httpx.MockTransport(handler))
    embedding = client.embed("текст", "doc")

    assert embedding.vector == [0.6, 0.8] and embedding.num_tokens == 3
    assert calls["n"] == 2
    # У bge-m3 одна модель для документов и вопросов — и один ключ кэша.
    assert client.cache_uri("doc") == client.cache_uri("query") == "mws://bge-m3"


def test_provider_error_names_the_provider() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="model not found")

    with pytest.raises(ProviderError) as error:
        _client(httpx.MockTransport(handler)).complete(
            [Message(role=Role.USER, content="?")], model="gpt-oss-120b"
        )
    assert error.value.status == 404 and "mws" in str(error.value)


def test_missing_keys_are_named_not_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDRU_BASE_URL", "https://api.example/v1")
    monkeypatch.delenv("CLOUDRU_API_KEY", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)

    with pytest.raises(SystemExit, match="CLOUDRU_API_KEY"):
        OpenAICompatibleClient.from_env("cloudru")


class _FakeEncoder:
    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    def encode(self, sentences: list[str], **kwargs: Any) -> list[list[float]]:
        assert kwargs["normalize_embeddings"] is True
        self.batches.append(sentences)
        return [[float(len(s)), 1.0] for s in sentences]


def test_local_embedder_goes_in_batches_and_caches(tmp_path: Path) -> None:
    encoder = _FakeEncoder()
    embedder = LocalEmbedder("BAAI/bge-m3", model=encoder)
    cache = EmbeddingCache(tmp_path / "cache.sqlite")
    texts = [f"текст {i}" for i in range(40)]

    first = embed_many(embedder, texts, "doc", cache)
    again = embed_many(embedder, texts, "query", cache)

    # Пачки по 32, второй раз — из кэша (ключ один для doc и query).
    assert [len(b) for b in encoder.batches] == [32, 8]
    assert [e.vector for e in again] == [e.vector for e in first]
    assert embedder.model_uri("doc") == "local://BAAI/bge-m3"
    cache.close()


def test_embedding_client_by_model_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MWS_BASE_URL", "https://api.example/v1")
    monkeypatch.setenv("MWS_API_KEY", "key")
    monkeypatch.setenv("YC_FOLDER_ID", "folder")
    monkeypatch.setenv("YC_API_KEY", "key")

    mws = embedding_client("mws:bge-m3")
    local = embedding_client("local:BAAI/bge-m3")
    yandex = embedding_client("text-embeddings-v2", 768)

    assert isinstance(mws, OpenAICompatibleClient) and mws.provider == "mws"
    assert isinstance(local, LocalEmbedder) and local.model_name == "BAAI/bge-m3"
    assert isinstance(yandex, YandexClient)


def test_chat_with_tools_returns_tool_calls() -> None:
    # Агент (вариант 1): модель просит поиск — вызов инструмента, текста нет.
    seen: list[dict[str, Any]] = []
    # gpt-oss присылает кириллицу в аргументах экранами (ensure_ascii).
    arguments = json.dumps({"query": "отпуск"}, ensure_ascii=True)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "search_documents",
                                        "arguments": arguments,
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 12},
            },
        )

    tools = [{"type": "function", "function": {"name": "search_documents"}}]
    completion = _client(httpx.MockTransport(handler)).chat_with_tools(
        [{"role": "user", "content": "Сколько дней отпуска?"}],
        model="gpt-oss-120b",
        tools=tools,
    )

    assert seen[0]["tools"] == tools and seen[0]["tool_choice"] == "auto"
    assert completion.text == "" and completion.finish_reason == "tool_calls"
    (call,) = completion.tool_calls
    assert (call.id, call.name) == ("call_1", "search_documents")
    assert json.loads(call.arguments) == {"query": "отпуск"}
