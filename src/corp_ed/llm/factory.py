import asyncio

import httpx

from corp_ed.core.config import LLMSettings
from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.yandex import YandexAdapter
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
