"""Клиент Yandex AI Studio для eval-скриптов (не для прода).

Зачем свой, а не llm/yandex*.py бэкенда: бэкенд переписывается, а
eval-скриптам нужны синхронные вызовы, повторы с паузой при 429 и кэш
эмбеддингов на диске. Прод ходит в Яндекс только через код бэкенда.

Ключи — те же, что у бэкенда (.env): YC_FOLDER_ID, YC_API_KEY.

Эмбеддинги: text-search-doc для документов, text-search-query для
вопросов (пара моделей, перепутать — тихая потеря качества).
Генерация: нативный API completion (им пользуется бэкенд, только
модели YandexGPT) или OpenAI-совместимый /v1/chat/completions (api="openai"):
через него доступны все модели каталога AI Studio — Alice AI, DeepSeek,
gpt-oss, Qwen. У рассуждающих моделей скрытые «размышления» приходят в
usage как часть completion_tokens и тарифицируются как выход.
"""

import hashlib
import json
import os
import random
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

from corp_ed.llm.types import Message

EMBEDDING_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/textEmbedding"
COMPLETION_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
CHAT_URL = "https://llm.api.cloud.yandex.net/v1/chat/completions"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
EMBEDDING_RPS = 8.0
"""Квота AI Studio по умолчанию — 10 запросов эмбеддинга в секунду на
каталог (замер 24.09: 4 потока без ограничения получили 429). Берём с
запасом: квота общая с бэкендом, если он работает в том же каталоге."""

COMPLETION_RPS = 8.0
COMPLETION_CONCURRENCY = 8
"""Генерация: квота AI Studio — не больше 10 одновременных запросов на
каталог (замер 25.09: ai.textGenerationCompletionSessionsCount, «allowed
10 requests» → 429). Держим не больше 8 одновременных и не чаще 8 в
секунду, чтобы параллельные скрипты и бэкенд не упирались в квоту."""

EmbeddingKind = Literal["doc", "query"]
Api = Literal["native", "openai"]


@dataclass(frozen=True)
class Embedding:
    vector: list[float]
    num_tokens: int


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    model: str
    reasoning_tokens: int = 0
    """Скрытые токены рассуждения (входят в output_tokens)."""
    finish_reason: str = ""


class RateLimiter:
    """Не чаще rate запросов в секунду суммарно по всем потокам.

    Каждый вызов wait() занимает следующий свободный слот и спит до него.
    Повторы после 429 тоже проходят через слоты, иначе они снова упираются
    в квоту.
    """

    def __init__(
        self,
        rate: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self._interval = 1.0 / rate
        self._clock = clock
        self._sleep = sleep
        self._next_slot = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._interval
        if slot > now:
            self._sleep(slot - now)


class YandexError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"Yandex API {status}: {body[:500]}")
        self.status = status
        self.body = body


class YandexClient:
    def __init__(
        self,
        folder_id: str,
        api_key: str,
        *,
        http: httpx.Client | None = None,
        max_attempts: int = 5,
        base_delay: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        embedding_rps: float = EMBEDDING_RPS,
        embedding_model: str = "text-search",
        completion_rps: float = COMPLETION_RPS,
        completion_concurrency: int = COMPLETION_CONCURRENCY,
    ) -> None:
        self._folder_id = folder_id
        self._api_key = api_key
        self._http = http or httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0))
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._sleep = sleep
        self._embedding_limiter = RateLimiter(embedding_rps, sleep=sleep)
        self._embedding_model = embedding_model
        self._completion_limiter = RateLimiter(completion_rps, sleep=sleep)
        self._completion_slots = threading.BoundedSemaphore(completion_concurrency)

    @classmethod
    def from_env(cls, **options: Any) -> "YandexClient":
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:
            pass
        try:
            return cls(os.environ["YC_FOLDER_ID"], os.environ["YC_API_KEY"], **options)
        except KeyError as error:
            raise SystemExit(
                f"Не задан {error} (нужны YC_FOLDER_ID и YC_API_KEY)"
            ) from None

    def model_uri(self, kind: EmbeddingKind) -> str:
        """Пара моделей <семейство>-doc / <семейство>-query: text-search,
        text-embeddings-v2."""
        return f"emb://{self._folder_id}/{self._embedding_model}-{kind}/latest"

    def gpt_uri(self, model: str) -> str:
        """«yandexgpt-lite» → gpt://<каталог>/yandexgpt-lite/latest; версию
        можно указать явно: «yandexgpt/rc»."""
        return f"gpt://{self._folder_id}/{model if '/' in model else model + '/latest'}"

    def embed(self, text: str, kind: EmbeddingKind) -> Embedding:
        body = self._post(
            EMBEDDING_URL,
            {"modelUri": self.model_uri(kind), "text": text},
            limiter=self._embedding_limiter,
        )
        return Embedding(
            vector=[float(x) for x in body["embedding"]],
            num_tokens=int(body.get("numTokens", 0)),
        )

    def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str = "yandexgpt-lite",
        temperature: float = 0.3,
        max_tokens: int = 1000,
        api: Api = "native",
        response_format: dict[str, Any] | None = None,
    ) -> Completion:
        """response_format — как в OpenAI API: {"type": "json_object"} или
        {"type": "json_schema", "json_schema": {"name": …, "schema": …}}.
        Для native переводится в jsonObject / jsonSchema."""
        if api == "openai":
            return self._chat(messages, model, temperature, max_tokens, response_format)
        payload: dict[str, Any] = {
            "modelUri": self.gpt_uri(model),
            "completionOptions": {
                "stream": False,
                "temperature": temperature,
                "maxTokens": max_tokens,
            },
            "messages": [{"role": m.role.value, "text": m.content} for m in messages],
        }
        if response_format and response_format.get("type") == "json_object":
            payload["jsonObject"] = True
        elif response_format and response_format.get("type") == "json_schema":
            payload["jsonSchema"] = {"schema": response_format["json_schema"]["schema"]}
        with self._completion_slots:
            started = time.perf_counter()
            body = self._post(COMPLETION_URL, payload, limiter=self._completion_limiter)
            latency_ms = (time.perf_counter() - started) * 1000
        result = body["result"]
        usage = result.get("usage", {})
        return Completion(
            text=result["alternatives"][0]["message"]["text"],
            input_tokens=int(usage.get("inputTextTokens", 0)),
            output_tokens=int(usage.get("completionTokens", 0)),
            latency_ms=latency_ms,
            model=model,
            finish_reason=str(result["alternatives"][0].get("status", "")),
        )

    def _chat(
        self,
        messages: Sequence[Message],
        model: str,
        temperature: float,
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
    ) -> Completion:
        payload: dict[str, Any] = {
            "model": self.gpt_uri(model),
            "messages": [
                {"role": m.role.value, "content": m.content} for m in messages
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format
        with self._completion_slots:
            started = time.perf_counter()
            body = self._post(
                CHAT_URL,
                payload,
                limiter=self._completion_limiter,
                extra_headers={"OpenAI-Project": self._folder_id},
            )
            latency_ms = (time.perf_counter() - started) * 1000
        choice = body["choices"][0]
        usage = body.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        return Completion(
            text=str(choice["message"].get("content") or "").strip(),
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            latency_ms=latency_ms,
            model=model,
            reasoning_tokens=int(details.get("reasoning_tokens") or 0),
            finish_reason=str(choice.get("finish_reason") or ""),
        )

    def _post(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        limiter: RateLimiter | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Api-Key {self._api_key}", **(extra_headers or {})}
        for attempt in range(self._max_attempts):
            if limiter:
                limiter.wait()
            try:
                response = self._http.post(url, json=payload, headers=headers)
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
                raise YandexError(response.status_code, response.text)
            data: dict[str, Any] = response.json()
            return data
        raise RuntimeError("unreachable")

    def _pause(self, attempt: int) -> None:
        # Множитель, а не добавка: разброс не теряет долю с ростом номера
        # попытки (см. RISKS.md бэкенда, пункт про jitter).
        self._sleep(self._base_delay * 2**attempt * random.uniform(0.5, 1.5))


class EmbeddingCache:
    """Кэш эмбеддингов в SQLite: повторный прогон не платит за те же тексты.

    Ключ — модель + текст. E2 (крошки в embed_text / без) и E3 (сетка
    размеров) пересекаются по текстам слабо, а вот повторы одного
    эксперимента и вопросы набора кэшируются полностью.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS embeddings "
            "(key TEXT PRIMARY KEY, vector TEXT NOT NULL, num_tokens INTEGER NOT NULL)"
        )

    @staticmethod
    def key(model_uri: str, text: str) -> str:
        return hashlib.sha256(f"{model_uri}\n{text}".encode()).hexdigest()

    def get(self, model_uri: str, text: str) -> Embedding | None:
        row = self._db.execute(
            "SELECT vector, num_tokens FROM embeddings WHERE key = ?",
            (self.key(model_uri, text),),
        ).fetchone()
        if row is None:
            return None
        return Embedding(vector=json.loads(row[0]), num_tokens=int(row[1]))

    def put(self, model_uri: str, text: str, embedding: Embedding) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO embeddings VALUES (?, ?, ?)",
            (
                self.key(model_uri, text),
                json.dumps(embedding.vector),
                embedding.num_tokens,
            ),
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()


def embed_many(
    client: YandexClient,
    texts: Sequence[str],
    kind: EmbeddingKind,
    cache: EmbeddingCache | None = None,
    *,
    workers: int = 4,
    progress: Callable[[int, int], None] | None = None,
) -> list[Embedding]:
    """Эмбеддинги списка текстов: из кэша, остальное — параллельно в API.

    Частоту держит RateLimiter клиента (квота 10 rps), потоки нужны, чтобы
    при задержке ~130 мс на запрос эту квоту выбрать.
    """
    from concurrent.futures import ThreadPoolExecutor

    model_uri = client.model_uri(kind)
    results: list[Embedding | None] = [
        cache.get(model_uri, text) if cache else None for text in texts
    ]
    missing = [i for i, result in enumerate(results) if result is None]
    done = len(texts) - len(missing)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {i: pool.submit(client.embed, texts[i], kind) for i in missing}
        for i, future in futures.items():
            embedding = future.result()
            results[i] = embedding
            if cache:
                cache.put(model_uri, texts[i], embedding)
            done += 1
            if progress:
                progress(done, len(texts))

    return [result for result in results if result is not None]
