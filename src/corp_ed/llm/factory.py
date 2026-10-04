import asyncio

import httpx

from corp_ed.core.config import LLMSettings
from corp_ed.llm.embedding_gateway import EmbeddingGateway
from corp_ed.llm.fake import DevAdapter
from corp_ed.llm.fake_embedding import WordEmbeddingAdapter
from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.throttle import Throttle
from corp_ed.llm.yandex import YandexAdapter
from corp_ed.llm.yandex_embedding import YandexEmbeddingAdapter
from corp_ed.llm.yandex_openai import YandexOpenAIAdapter


def build_llm_gateway(
    client: httpx.AsyncClient,
    settings: LLMSettings,
    concurrency: asyncio.Semaphore | None = None,
) -> LLMGateway:
    """Адаптер по настройке LLM_PROVIDER — для API и для ночных задач.

    Тип возврата — контракт: вызывающий код не знает, какой адаптер ему
    достался.
    """
    if settings.llm_provider == "fake":
        return DevAdapter(
            stream_delay=settings.llm_fake_stream_delay_ms / 1000,
            latency=settings.llm_fake_latency_ms / 1000,
            concurrency=concurrency if settings.llm_fake_quotas else None,
        )
    if settings.llm_provider == "yandex-native":
        return YandexAdapter(
            client=client,
            folder_id=settings.yc_folder_id,
            api_key=settings.yc_api_key.get_secret_value(),
            model=settings.llm_model,
            concurrency=concurrency,
        )
    return YandexOpenAIAdapter(
        client=client,
        folder_id=settings.yc_folder_id,
        api_key=settings.yc_api_key.get_secret_value(),
        model=settings.llm_model,
        concurrency=concurrency,
    )


def build_embedding_gateway(
    client: httpx.AsyncClient,
    settings: LLMSettings,
    *,
    document_throttle: Throttle | None = None,
    query_throttle: Throttle | None = None,
) -> EmbeddingGateway:
    """Эмбеддер по LLM_PROVIDER: Yandex или «мешок слов» в режиме fake."""
    if settings.llm_provider == "fake":
        if not settings.llm_fake_quotas:
            return WordEmbeddingAdapter()
        return WordEmbeddingAdapter(
            document_throttle=document_throttle, query_throttle=query_throttle
        )
    return YandexEmbeddingAdapter(
        client=client,
        folder_id=settings.yc_folder_id,
        api_key=settings.yc_api_key.get_secret_value(),
        family=settings.embedding_model,
        dim=settings.embedding_dim,
        document_throttle=document_throttle,
        query_throttle=query_throttle,
    )
