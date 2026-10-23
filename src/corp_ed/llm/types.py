from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class FinishReason(StrEnum):
    COMPLETED = "completed"
    TRUNCATED = "truncated"
    # Модель ответила, но провайдер пометил ответ фильтром содержимого
    # (content_filter). Это не сбой: повтор даст то же самое (BH-25).
    FILTERED = "filtered"


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class Completion:
    content: str
    finish_reason: FinishReason
    usage: Usage
    model_version: str
    model: str
    latency_ms: int
    provider: str = ""
    """Кто ответил: yandex, cloudru (BH-50); пусто — тестовая модель."""
    fallback_reason: str | None = None
    """Ответ запасной модели — почему основная не ответила (ErrorKind или
    breaker_open); None — ответила основная."""


@dataclass(frozen=True)
class EmbeddingResult:
    embedding: list[float]
    input_tokens: int
    model_version: str
    model: str
    latency_ms: int
