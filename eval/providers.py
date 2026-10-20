"""Другие поставщики моделей для eval-скриптов (план резерва, 15.10).

Тот же интерфейс, что у eval.yandex.YandexClient (complete, embed,
model_uri, cache_uri), поэтому offline_e2e и bench меняют поставщика
одним флагом, а не своим кодом:
- OpenAICompatibleClient — MWS GPT и Cloud.ru Foundation Models по
  OpenAI-совместимому API: /chat/completions и /embeddings, ключ в
  заголовке Authorization: Bearer;
- LocalEmbedder — открытые веса через sentence-transformers (bge-m3 на
  своём процессоре, Р-3 «в»); пакета в зависимостях проекта нет —
  запускать из sandbox/.venv-rerank, как реранкер (eval/rerank.py).

Ключи и адреса — только из .env: <ПОСТАВЩИК>_API_KEY, <ПОСТАВЩИК>_BASE_URL
(например MWS_API_KEY, MWS_BASE_URL). Имя модели уходит поставщику как есть.
"""

import os
import random
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any, Protocol

import httpx

from corp_ed.llm.types import Message
from eval.yandex import (
    RETRYABLE_STATUS,
    Completion,
    Embedding,
    EmbeddingKind,
    RateLimiter,
    YandexClient,
)

PROVIDERS = ("mws", "cloudru")
"""Поставщики с OpenAI-совместимым API; имя — префикс переменных .env."""

PROVIDER_RPS = 5.0
PROVIDER_CONCURRENCY = 5
"""Квоты MWS и Cloud.ru не известны до аккаунта — берём скромно; уточнить
на этапе 0 плана и поднять."""


class ProviderError(RuntimeError):
    def __init__(self, provider: str, status: int, body: str) -> None:
        super().__init__(f"{provider} API {status}: {body[:500]}")
        self.provider = provider
        self.status = status
        self.body = body


class OpenAICompatibleClient:
    """Синхронный клиент /chat/completions и /embeddings с повторами при 429."""

    def __init__(
        self,
        provider: str,
        base_url: str,
        api_key: str,
        *,
        http: httpx.Client | None = None,
        max_attempts: int = 5,
        base_delay: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        rps: float = PROVIDER_RPS,
        concurrency: int = PROVIDER_CONCURRENCY,
        embedding_model: str = "bge-m3",
    ) -> None:
        self.provider = provider
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._http = http or httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0))
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._sleep = sleep
        self._limiter = RateLimiter(rps, sleep=sleep)
        self._slots = threading.BoundedSemaphore(concurrency)
        self._embedding_model = embedding_model

    @classmethod
    def from_env(cls, provider: str, **options: Any) -> "OpenAICompatibleClient":
        if provider not in PROVIDERS:
            raise SystemExit(
                f"Неизвестный поставщик {provider!r}: {', '.join(PROVIDERS)}"
            )
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:
            pass
        prefix = provider.upper()
        base_url = os.environ.get(f"{prefix}_BASE_URL", "")
        api_key = os.environ.get(f"{prefix}_API_KEY", "")
        missing = [
            name
            for name, value in (
                (f"{prefix}_BASE_URL", base_url),
                (f"{prefix}_API_KEY", api_key),
            )
            if not value
        ]
        if missing:
            raise SystemExit(f"Не задано в .env: {', '.join(missing)}")
        return cls(provider, base_url, api_key, **options)

    def model_uri(self, kind: EmbeddingKind) -> str:
        """У bge-m3 одна модель и для документов, и для вопросов."""
        return f"{self.provider}://{self._embedding_model}"

    def cache_uri(self, kind: EmbeddingKind) -> str:
        return self.model_uri(kind)

    def embed(self, text: str, kind: EmbeddingKind) -> Embedding:
        body = self._post(
            "/embeddings", {"model": self._embedding_model, "input": text}
        )
        usage = body.get("usage") or {}
        return Embedding(
            vector=[float(x) for x in body["data"][0]["embedding"]],
            num_tokens=int(
                usage.get("prompt_tokens") or usage.get("total_tokens") or 0
            ),
        )

    def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 1000,
        api: str = "openai",
        response_format: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Completion:
        """Как YandexClient.complete с api="openai"; api другим быть не может."""
        if api != "openai":
            raise ValueError(f"{self.provider}: только OpenAI-совместимый API")
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": m.role.value, "content": m.content} for m in messages
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format
        if extra:
            payload.update(extra)
        with self._slots:
            started = time.perf_counter()
            body = self._post("/chat/completions", payload)
            latency_ms = (time.perf_counter() - started) * 1000
        choice = body["choices"][0]
        usage = body.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        return Completion(
            text=str(choice["message"].get("content") or "").strip(),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            latency_ms=latency_ms,
            model=model,
            reasoning_tokens=int(details.get("reasoning_tokens") or 0),
            finish_reason=str(choice.get("finish_reason") or ""),
        )

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        for attempt in range(self._max_attempts):
            self._limiter.wait()
            try:
                response = self._http.post(
                    self._base_url + path, json=payload, headers=headers
                )
            except httpx.TransportError:
                if attempt == self._max_attempts - 1:
                    raise
                self._pause(attempt)
                continue
            if (
                response.status_code in RETRYABLE_STATUS
                and attempt < self._max_attempts - 1
            ):
                self._pause(attempt)
                continue
            if response.status_code >= 400:
                raise ProviderError(self.provider, response.status_code, response.text)
            data: dict[str, Any] = response.json()
            return data
        raise RuntimeError("unreachable")

    def _pause(self, attempt: int) -> None:
        self._sleep(self._base_delay * 2**attempt * random.uniform(0.5, 1.5))


class _SentenceEncoder(Protocol):
    def encode(self, sentences: list[str], **kwargs: Any) -> Any: ...


class LocalEmbedder:
    """Эмбеддинги открытой модели на своём процессоре (sentence-transformers).

    Векторы нормированы; один и тот же текст — один вектор и для документа,
    и для вопроса (у bge-m3 dense нет инструкций). embed_batch — пачками:
    так в разы быстрее, чем по одному тексту в потоке.
    """

    def __init__(
        self, model_name: str, *, model: _SentenceEncoder | None = None
    ) -> None:
        self.model_name = model_name
        self._model = model
        self._lock = threading.Lock()
        self.encode_seconds = 0.0
        """Сколько процессорного времени ушло на кодирование (Р-3 «в»)."""

    def _encoder(self) -> _SentenceEncoder:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name, device="cpu")
        return self._model

    def model_uri(self, kind: EmbeddingKind) -> str:
        return f"local://{self.model_name}"

    def cache_uri(self, kind: EmbeddingKind) -> str:
        return self.model_uri(kind)

    def embed(self, text: str, kind: EmbeddingKind) -> Embedding:
        return self.embed_batch([text], kind)[0]

    def embed_batch(self, texts: Sequence[str], kind: EmbeddingKind) -> list[Embedding]:
        with self._lock:
            started = time.perf_counter()
            vectors = self._encoder().encode(
                list(texts), batch_size=8, normalize_embeddings=True
            )
            self.encode_seconds += time.perf_counter() - started
        return [Embedding(vector=[float(x) for x in v], num_tokens=0) for v in vectors]


def embedding_client(
    embedding_model: str, embedding_dim: int | None = None
) -> YandexClient | OpenAICompatibleClient | LocalEmbedder:
    """Клиент эмбеддингов по имени модели:
    «mws:bge-m3», «cloudru:bge-m3» — поставщик и модель;
    «local:BAAI/bge-m3» — открытые веса на своём процессоре;
    иначе — семейство Яндекса (text-embeddings-v2, text-search)."""
    prefix, _, name = embedding_model.partition(":")
    if name and prefix in PROVIDERS:
        return OpenAICompatibleClient.from_env(prefix, embedding_model=name)
    if name and prefix == "local":
        return LocalEmbedder(name)
    return YandexClient.from_env(
        embedding_model=embedding_model, embedding_dim=embedding_dim
    )
