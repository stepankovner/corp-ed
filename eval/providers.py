"""Другие поставщики моделей для eval-скриптов (план резерва, 15.10).

Тот же интерфейс, что у eval.yandex.YandexClient (complete, embed,
model_uri, cache_uri), поэтому offline_e2e и bench меняют поставщика
одним флагом, а не своим кодом:
- OpenAICompatibleClient — MWS GPT и Cloud.ru Foundation Models по
  OpenAI-совместимому API: /chat/completions и /embeddings, ключ в
  заголовке Authorization: Bearer;
- DecisionChain — цепочка поставщиков шага решения (Cloud.ru → MWS, BH-46
  «Поставщики решения», 22.10): переход к следующему звену при сбое,
  предохранитель на звено, журнал событий;
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
from dataclasses import dataclass, field
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
    parse_chat_body,
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


class CallTimeoutError(TimeoutError):
    """Вызов не уложился в таймаут complete(timeout=…) — без повторов.

    elapsed_ms — сколько прошло с начала вызова (для времени по стене).
    Соединение закрывает клиент; продолжает ли поставщик генерацию и берёт
    ли за неё деньги — не известно, токенов прерванного вызова нет."""

    def __init__(self, provider: str, elapsed_ms: float) -> None:
        super().__init__(f"{provider}: таймаут вызова, {elapsed_ms:.0f} мс")
        self.provider = provider
        self.elapsed_ms = elapsed_ms


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
        timeout: float | None = None,
    ) -> Completion:
        """Как YandexClient.complete с api="openai"; api другим быть не может.

        timeout (с) — срок на весь вызов (фаза 1, таймаут шага решения): таймаут
        запроса httpx — остаток срока, повторов после таймаута нет (иначе
        таймаут чтения — подкласс TransportError — повторялся бы до 5 раз),
        по истечении — CallTimeoutError. Срок держится на соединение, запись и
        чтение по отдельности: для ответа без стрима это почти всё время."""
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
            body = self._post("/chat/completions", payload, timeout=timeout)
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

    def chat_with_tools(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        model: str,
        tools: Sequence[dict[str, Any]],
        temperature: float = 0.0,
        max_tokens: int = 1000,
        tool_choice: str = "auto",
        extra: dict[str, Any] | None = None,
    ) -> Completion:
        """Как YandexClient.chat_with_tools — у поставщика по OpenAI API."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # Пустые инструменты — вызов без них (последний после лимита доборов):
        # tool_choice="none" у Flash пишет вызов текстом в ответ (20.10).
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = tool_choice
        if extra:
            payload.update(extra)
        with self._slots:
            started = time.perf_counter()
            body = self._post("/chat/completions", payload)
            latency_ms = (time.perf_counter() - started) * 1000
        return parse_chat_body(body, model, latency_ms)

    def _post(
        self, path: str, payload: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        started = time.perf_counter()
        deadline = None if timeout is None else started + timeout

        def expired() -> CallTimeoutError:
            return CallTimeoutError(
                self.provider, (time.perf_counter() - started) * 1000
            )

        for attempt in range(self._max_attempts):
            self._limiter.wait()
            options: dict[str, Any] = {}
            if deadline is not None:
                left = deadline - time.perf_counter()
                if left <= 0:
                    raise expired()
                options["timeout"] = httpx.Timeout(left)
            try:
                response = self._http.post(
                    self._base_url + path, json=payload, headers=headers, **options
                )
            except httpx.TimeoutException:
                if deadline is not None:
                    raise expired() from None
                if attempt == self._max_attempts - 1:
                    raise
                self._pause(attempt)
                continue
            except httpx.TransportError:
                if attempt == self._max_attempts - 1:
                    raise
                self._pause(attempt, deadline)
                continue
            if (
                response.status_code in RETRYABLE_STATUS
                and attempt < self._max_attempts - 1
            ):
                self._pause(attempt, deadline)
                continue
            if response.status_code >= 400:
                raise ProviderError(self.provider, response.status_code, response.text)
            data: dict[str, Any] = response.json()
            return data
        raise RuntimeError("unreachable")

    def _pause(self, attempt: int, deadline: float | None = None) -> None:
        delay = self._base_delay * 2**attempt * random.uniform(0.5, 1.5)
        if deadline is not None:
            # Пауза не дольше срока: после неё следующий заход сам увидит,
            # что срок вышел, и бросит CallTimeoutError.
            delay = max(0.0, min(delay, deadline - time.perf_counter()))
        self._sleep(delay)


CONFIG_STATUS = frozenset({400, 401, 402, 403, 404})
"""Ошибки настройки (handoff BH-46 «Поставщики решения» п. 3): Cloud.ru —
неверный ключ 400, ключ не того уровня 401, нет денег 402, нет модели 404;
MWS — нет прав 403, нет деплоймента 404. Переход ко второму звену и
тревога сразу."""

MAYBE_CONFIG_STATUS = {"mws": frozenset({500})}
"""MWS отвечает 500 `INTERNAL` и на неверный ключ (15.10): и ошибка, и
возможная настройка — в журнал поставщик и код, без тревоги."""


def classify_failure(provider: str, exc: BaseException) -> tuple[str, int | None]:
    """Вид сбоя звена и код ответа (если был).

    timeout — не уложился в срок (CallTimeoutError, таймаут httpx);
    transport — обрыв связи; overload — 429; server — 5xx;
    server_or_config — 5xx, которым поставщик отвечает и на неверный ключ
    (MWS 500); config — ошибка настройки (CONFIG_STATUS); request — прочие
    4xx (запрос не принят). Любой вид — переход к следующему звену."""
    if isinstance(exc, (CallTimeoutError, httpx.TimeoutException)):
        return "timeout", None
    if isinstance(exc, httpx.TransportError):
        return "transport", None
    if isinstance(exc, ProviderError):
        status = exc.status
        if status in CONFIG_STATUS:
            return "config", status
        if status in MAYBE_CONFIG_STATUS.get(provider, frozenset()):
            return "server_or_config", status
        if status == 429:
            return "overload", status
        if status >= 500:
            return "server", status
        return "request", status
    raise exc


class CircuitBreaker:
    """Предохранитель звена: threshold сбоев подряд за window секунд —
    звено открыто на cooldown секунд (вызовы идут мимо него); по истечении
    — одна пробная попытка (полуоткрыто): успех закрывает, неудача —
    ещё cooldown. Ориентир handoff: 5 сбоев, 60 с, 120 с."""

    def __init__(
        self,
        threshold: int = 5,
        window: float = 60.0,
        cooldown: float = 120.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.threshold = threshold
        self.window = window
        self.cooldown = cooldown
        self._clock = clock
        self._failures: list[float] = []
        self._opened_at: float | None = None
        self._trial = False
        self._lock = threading.Lock()

    def state(self) -> str:
        with self._lock:
            return self._state()

    def _state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at < self.cooldown:
            return "open"
        return "half_open"

    def allow(self) -> bool:
        """Можно ли звать звено сейчас; в полуоткрытом — только одна проба."""
        with self._lock:
            state = self._state()
            if state == "closed":
                return True
            if state == "open" or self._trial:
                return False
            self._trial = True
            return True

    def success(self) -> None:
        with self._lock:
            self._failures.clear()
            self._opened_at = None
            self._trial = False

    def failure(self) -> None:
        with self._lock:
            now = self._clock()
            if self._trial or self._opened_at is not None:
                # Проба не прошла — ещё cooldown.
                self._opened_at = now
                self._trial = False
                return
            self._failures = [t for t in self._failures if now - t < self.window]
            self._failures.append(now)
            if len(self._failures) >= self.threshold:
                self._opened_at = now
                self._failures.clear()


class ChainExhaustedError(RuntimeError):
    """Ни одно звено цепочки не ответило (все упали или пропущены
    предохранителем) — вызывающий идёт обычным путём ответа."""


@dataclass(frozen=True)
class ChainEvent:
    """Строка журнала цепочки: звено, исход, код, состояние предохранителя
    до вызова, тревога (ошибка настройки), время."""

    provider: str
    outcome: str
    """ok | skipped (предохранитель открыт) | вид сбоя classify_failure."""
    status: int | None
    breaker: str
    alert: bool
    ms: float

    def as_list(self) -> list[Any]:
        return [self.provider, self.outcome, self.status, self.breaker, round(self.ms)]


@dataclass
class ChainLink:
    provider: str
    client: Any
    """Клиент с complete(...) как у OpenAICompatibleClient."""
    model: str
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)


class DecisionChain:
    """Цепочка поставщиков шага решения для eval (BH-46, «Поставщики
    решения»): звенья по порядку; сбой (5xx, 429, обрыв, таймаут, ошибка
    настройки) — сразу следующее звено с остатком срока; у каждого звена
    свой предохранитель. Срок timeout — на весь вызов цепочки (п. 5
    handoff), link_timeout — потолок одного звена (без него первое звено
    может съесть весь срок). Все звенья недоступны — ChainExhaustedError,
    срок вышел — CallTimeoutError: в обоих случаях цикл решения идёт
    обычным путём ответа. Журнал — events / drain(); модель у каждого звена
    своя, аргумент model вызова не используется.

    Клиенты звеньев — с max_attempts=1: свои повторы клиента съели бы
    время до перехода (в продукте — то же: одна попытка на звено)."""

    def __init__(
        self,
        links: Sequence[ChainLink],
        *,
        link_timeout: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not links:
            raise ValueError("цепочка без звеньев")
        self.links = list(links)
        self.link_timeout = link_timeout
        self._clock = clock
        self._events: list[ChainEvent] = []
        self._lock = threading.Lock()

    def _log(self, event: ChainEvent) -> None:
        with self._lock:
            self._events.append(event)

    @property
    def events(self) -> list[ChainEvent]:
        with self._lock:
            return list(self._events)

    def drain(self) -> list[ChainEvent]:
        """События с прошлого drain (offline_e2e — после каждого вопроса)."""
        with self._lock:
            out, self._events = self._events, []
        return out

    def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str = "",
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Completion:
        started = self._clock()
        deadline = None if timeout is None else started + timeout
        for link in self.links:
            state = link.breaker.state()
            limit = self.link_timeout
            if deadline is not None:
                left = deadline - self._clock()
                if left <= 0:
                    break
                limit = left if limit is None else min(limit, left)
            if not link.breaker.allow():
                self._log(ChainEvent(link.provider, "skipped", None, state, False, 0.0))
                continue
            call_started = self._clock()
            try:
                completion: Completion = link.client.complete(
                    messages,
                    model=link.model,
                    **({"timeout": limit} if limit is not None else {}),
                    **kwargs,
                )
            except (ProviderError, CallTimeoutError, httpx.TransportError) as exc:
                kind, status = classify_failure(link.provider, exc)
                link.breaker.failure()
                elapsed = getattr(exc, "elapsed_ms", None)
                self._log(
                    ChainEvent(
                        link.provider,
                        kind,
                        status,
                        state,
                        kind == "config",
                        float(elapsed)
                        if elapsed is not None
                        else (self._clock() - call_started) * 1000,
                    )
                )
                continue
            link.breaker.success()
            self._log(
                ChainEvent(
                    link.provider, "ok", None, state, False, completion.latency_ms
                )
            )
            return completion
        if deadline is not None and self._clock() >= deadline:
            raise CallTimeoutError("chain", (self._clock() - started) * 1000)
        raise ChainExhaustedError("ни одно звено цепочки решения не ответило")


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
