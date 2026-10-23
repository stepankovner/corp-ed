import asyncio

import httpx

from corp_ed.core.config import LLMSettings, RagSettings
from corp_ed.domain.decision import parse_decision
from corp_ed.llm.decision_client import HedgedDecider, cloudru_leg, yandex_leg
from corp_ed.llm.embedding_breaker import BreakerEmbeddings
from corp_ed.llm.embedding_gateway import EmbeddingGateway
from corp_ed.llm.fake import DevAdapter
from corp_ed.llm.fake_embedding import WordEmbeddingAdapter
from corp_ed.llm.fallback import CircuitBreaker, FallbackGateway
from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.probes import Prober
from corp_ed.llm.throttle import Throttle
from corp_ed.llm.yandex import YandexAdapter
from corp_ed.llm.yandex_embedding import YandexEmbeddingAdapter
from corp_ed.llm.yandex_openai import CloudRuAdapter, YandexOpenAIAdapter

_breaker: CircuitBreaker | None = None
_embedding_breaker: CircuitBreaker | None = None


def shared_breaker(settings: LLMSettings) -> CircuitBreaker:
    """Предохранитель основной модели — один на процесс (BH-50): адаптеры
    создаются на запрос, а состояние «Яндекс лежит» общее."""
    global _breaker
    if _breaker is None:
        _breaker = CircuitBreaker(
            failures=settings.llm_breaker_failures,
            open_seconds=settings.llm_breaker_open_seconds,
        )
    return _breaker


def shared_embedding_breaker(settings: LLMSettings) -> CircuitBreaker:
    """Предохранитель эмбеддингов — свой, один на процесс (BH-51)."""
    global _embedding_breaker
    if _embedding_breaker is None:
        _embedding_breaker = CircuitBreaker(
            failures=settings.llm_breaker_failures,
            open_seconds=settings.llm_breaker_open_seconds,
            kind="embedding",
        )
    return _embedding_breaker


def build_llm_gateway(
    client: httpx.AsyncClient,
    settings: LLMSettings,
    concurrency: asyncio.Semaphore | None = None,
    *,
    reserve: bool = True,
) -> LLMGateway:
    """Адаптер по настройке LLM_PROVIDER — для API и для ночных задач.

    Тип возврата — контракт: вызывающий код не знает, какой адаптер ему
    достался. С ключом Cloud.ru — основная модель с запасной и
    предохранителем (BH-50). Ночные подписи пробелов зовут с
    reserve=False: ответа никто не ждёт — только предохранитель, при
    открытом подписи пропускаются до следующего запуска.
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
    deadlines = {
        "max_attempts": settings.llm_max_attempts,
        "read_timeout": settings.llm_read_timeout_seconds,
        "retry_on_timeout": False,
        "max_delay": settings.llm_retry_max_delay_seconds,
        "call_timeout": settings.llm_read_timeout_seconds,
        "stream_timeout": settings.llm_stream_timeout_seconds,
    }
    primary = YandexOpenAIAdapter(
        client=client,
        folder_id=settings.yc_folder_id,
        api_key=settings.yc_api_key.get_secret_value(),
        model=settings.llm_model,
        concurrency=concurrency,
        **deadlines,  # type: ignore[arg-type]
    )
    if not reserve:
        return FallbackGateway(primary, None, shared_breaker(settings))
    if not settings.fallback_enabled:
        return primary
    backup = CloudRuAdapter(
        client=client,
        base_url=settings.cloudru_base_url,
        api_key=settings.cloudru_api_key.get_secret_value(),
        model=settings.llm_fallback_model,
        **deadlines,
    )
    return FallbackGateway(primary, backup, shared_breaker(settings))


def build_embedding_gateway(
    client: httpx.AsyncClient,
    settings: LLMSettings,
    *,
    document_throttle: Throttle | None = None,
    query_throttle: Throttle | None = None,
) -> EmbeddingGateway:
    """Эмбеддер по LLM_PROVIDER: Yandex или «мешок слов» в режиме fake.

    Yandex — с коротким сроком вопроса и предохранителем (BH-51)."""
    if settings.llm_provider == "fake":
        if not settings.llm_fake_quotas:
            return WordEmbeddingAdapter()
        return WordEmbeddingAdapter(
            document_throttle=document_throttle, query_throttle=query_throttle
        )
    adapter = YandexEmbeddingAdapter(
        client=client,
        folder_id=settings.yc_folder_id,
        api_key=settings.yc_api_key.get_secret_value(),
        family=settings.embedding_model,
        dim=settings.embedding_dim,
        document_throttle=document_throttle,
        query_throttle=query_throttle,
        query_timeout=settings.embedding_query_timeout_seconds,
        query_attempts=settings.llm_max_attempts,
        max_delay=settings.llm_retry_max_delay_seconds,
    )
    return BreakerEmbeddings(adapter, shared_embedding_breaker(settings))


def build_prober(client: httpx.AsyncClient, settings: LLMSettings) -> Prober | None:
    """Пробные вызовы для процесса API (BH-52); в режиме fake — нет.

    Пробы идут мимо резерва и предохранителя-обёртки: прямо в адаптеры,
    а исход пишут в те же предохранители, что у запросов сотрудников."""
    if settings.llm_provider != "yandex-openai":
        return None
    chat = build_llm_gateway(client, settings)
    if isinstance(chat, FallbackGateway):
        primary, reserve = chat.primary, chat.reserve
    else:
        primary, reserve = chat, None
    embeddings = build_embedding_gateway(client, settings)
    inner = embeddings.inner if isinstance(embeddings, BreakerEmbeddings) else None
    decision = (
        cloudru_leg(
            client,
            settings.cloudru_base_url,
            settings.cloudru_api_key.get_secret_value(),
            settings.llm_decision_model,
        )
        if settings.fallback_enabled
        else None
    )
    return Prober(
        chat=primary,
        chat_breaker=shared_breaker(settings),
        embeddings=inner,
        embedding_breaker=shared_embedding_breaker(settings),
        reserve=reserve,
        decision=decision,
    )


def build_decider(
    client: httpx.AsyncClient, settings: LLMSettings, rag: RagSettings
) -> HedgedDecider | None:
    """Поставщики шага решения (BH-46): Cloud.ru основным, Qwen у Яндекса —
    дублёр. Без ключа Cloud.ru схемы нет (None): режимы shadow и on
    работают как off — выдержки уходят Cloud.ru только по договору."""
    if settings.llm_provider != "yandex-openai" or not settings.fallback_enabled:
        return None
    primary = cloudru_leg(
        client,
        settings.cloudru_base_url,
        settings.cloudru_api_key.get_secret_value(),
        settings.llm_decision_model,
    )
    hedge = yandex_leg(
        client,
        settings.yc_folder_id,
        settings.yc_api_key.get_secret_value(),
        settings.llm_decision_hedge_model,
    )
    return HedgedDecider(
        primary,
        hedge,
        delays={
            "low": rag.faq_decision_hedge_low_seconds,
            "medium": rag.faq_decision_hedge_medium_seconds,
        },
        valid=lambda text: parse_decision(text) is not None,
    )
