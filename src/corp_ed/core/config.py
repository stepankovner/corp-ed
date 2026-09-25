from functools import lru_cache
from typing import Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_SECRET_KEY_LENGTH = 32
"""HS256 подписывает ключом произвольной длины, но ключ короче размера
хеша (32 байта) перебирается офлайн по одному перехваченному токену.
`openssl rand -hex 32` даёт 64 символа."""


class Settings(BaseSettings):
    """Конфигурация приложения. Значения читаются из переменных окружения / .env."""

    # Окружение
    environment: str = "development"
    debug: bool = False
    # SecretStr: repr и логи показывают «**********», а не ключ.
    secret_key: SecretStr

    # Токены. Дефолты — решение по безопасности, а не по ML, поэтому они
    # здесь есть: короткий access ограничивает окно украденного токена,
    # refresh ротируется при каждом использовании (см. DECISIONS.md).
    access_token_ttl_minutes: int = Field(default=15, gt=0, le=60)
    refresh_token_ttl_days: int = Field(default=14, gt=0, le=90)

    # База данных
    database_url: str

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("secret_key")
    @classmethod
    def validate_secret_key(cls, value: SecretStr) -> SecretStr:
        # Падать на старте: со слабым ключом токены подделываются, и
        # это не видно ни в одном логе.
        if len(value.get_secret_value()) < MIN_SECRET_KEY_LENGTH:
            raise ValueError(
                f"SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} characters"
            )
        return value

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


class LLMSettings(BaseSettings):
    """Настройки провайдера языковых моделей."""

    yc_folder_id: str
    yc_api_key: str

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


class RagSettings(BaseSettings):
    """Параметры нарезки, поиска и ответа.

    Значения — зона ML (Артём). Здесь только проводка: дефолтов нет
    намеренно, чтобы придуманные числа не стали продакшен-значениями.

    Размеры — в ТОКЕНАХ в единицах count_tokens (len/3), а не в
    символах: у эмбеддера окно в токенах (BH-9).
    """

    chunk_tokens: int = Field(gt=0)
    overlap_tokens: int = Field(ge=0)
    faq_limit: int = Field(gt=0, le=50)
    faq_max_distance: float = Field(gt=0, le=2)
    context_max_tokens: int = Field(gt=0)
    # Для FAQ 0: при 0.3 ответ на один и тот же вопрос по одним и тем же
    # выдержкам переключался «ответил ↔ отказал» (замер ML 24.09, BH-8).
    faq_temperature: float = Field(ge=0, le=1)

    model_config = SettingsConfigDict(
        env_prefix="RAG_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_overlap(self) -> Self:
        # split_document сам кидает ValueError, но на первом ингесте.
        # Падать на старте дешевле, чем узнать об ошибке от клиента.
        if self.overlap_tokens >= self.chunk_tokens:
            raise ValueError("overlap_tokens must be less than chunk_tokens")
        return self
