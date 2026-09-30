"""Р-3 (RISKS №48): каждый запрос к Яндексу просит не сохранять данные.

По умолчанию Yandex AI Studio сохраняет запросы для улучшения сервиса, а
в них — выдержки из документов компании и вопросы сотрудников. Проверяем
все три адаптера: OpenAI-совместимый, нативный и эмбеддинги.
"""

from typing import Any

import httpx

from corp_ed.core.config import EMBEDDING_DIM
from corp_ed.llm.types import Message, Role
from corp_ed.llm.yandex import YandexAdapter
from corp_ed.llm.yandex_embedding import YandexEmbeddingAdapter
from corp_ed.llm.yandex_openai import YandexOpenAIAdapter

MESSAGES = [Message(role=Role.USER, content="Сколько дней отпуска?")]


def _recording(body: dict[str, Any]) -> tuple[list[httpx.Request], httpx.AsyncClient]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body)

    return seen, httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _assert_no_logging(request: httpx.Request) -> None:
    assert request.headers["x-data-logging-enabled"] == "false"
    assert request.headers["Authorization"] == "Api-Key k"


async def test_openai_compatible_adapter_disables_logging() -> None:
    seen, client = _recording(
        {
            "model": "aliceai-llm-flash/latest",
            "choices": [{"message": {"content": "28"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    )
    adapter = YandexOpenAIAdapter(
        client=client, folder_id="f", api_key="k", model="aliceai-llm-flash"
    )

    await adapter.generate(MESSAGES)

    _assert_no_logging(seen[0])


async def test_native_adapter_disables_logging() -> None:
    seen, client = _recording(
        {
            "result": {
                "alternatives": [
                    {
                        "message": {"role": "assistant", "text": "28"},
                        "status": "ALTERNATIVE_STATUS_FINAL",
                    }
                ],
                "usage": {"inputTextTokens": "1", "completionTokens": "1"},
                "modelVersion": "1",
            }
        }
    )

    await YandexAdapter(client=client, folder_id="f", api_key="k").generate(MESSAGES)

    _assert_no_logging(seen[0])


async def test_embeddings_disable_logging() -> None:
    seen, client = _recording(
        {"embedding": [0.1] * EMBEDDING_DIM, "numTokens": "2", "modelVersion": "1"}
    )
    adapter = YandexEmbeddingAdapter(client=client, folder_id="f", api_key="k")

    await adapter.embed_document("документ")
    await adapter.embed_query("вопрос")

    assert len(seen) == 2
    for request in seen:
        _assert_no_logging(request)
