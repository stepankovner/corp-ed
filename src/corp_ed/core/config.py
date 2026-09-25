from functools import lru_cache
from typing import Literal, Self

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
    """Настройки провайдера языковых моделей.

    Модель меняется одной переменной (досье 9.4): модели снимаются с
    поддержки (gpt-oss — 30.10.2026), а цены быстро меняются. Значения по
    умолчанию — решение ML от 25.09 (Alice AI LLM Flash), имена
    переменных согласованы с docs/backend-handoff.md.
    """

    yc_folder_id: str
    # SecretStr: ключ не попадёт в repr настроек и в лог.
    yc_api_key: SecretStr

    # yandex-openai — /v1/chat/completions (все модели каталога, в том числе
    # Flash); yandex-native — /foundationModels/v1/completion (только
    # YandexGPT, запасной вариант по досье 9.2).
    llm_provider: Literal["yandex-openai", "yandex-native"] = "yandex-openai"
    llm_model: str = "aliceai-llm-flash"
    # Квота генерации — 10 одновременных запросов на каталог. Лимит — на
    # процесс: при N воркерах uvicorn ставить не больше 10 / N.
    llm_max_concurrency: int = Field(default=8, gt=0, le=10)

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


class HttpSettings(BaseSettings):
    """Настройки HTTP-периметра: CORS, хосты, лимиты тела, документация.

    Отдельный класс, а не поля Settings: main.py читает их при сборке
    приложения, а Settings требует секретов — импорт приложения
    перестал бы работать без .env, и падали бы тесты и alembic. У
    каждого поля есть дефолт, безопасный для разработки; в production
    всё задаётся явно (см. .env.example).
    """

    environment: str = "development"

    # Строка через запятую, а не list[str]: сложные типы pydantic-settings
    # разбирает как JSON, и в переменной окружения пришлось бы писать
    # ["https://..."] — лишний источник опечаток при деплое.
    # Пусто — CORS выключен: браузер пустит только same-origin.
    cors_allowed_origins: str = ""
    # Host-заголовок: «*» годится только для разработки.
    allowed_hosts: str = "*"

    # Лимит тела запроса. JSON-ручкам мегабайта хватает с запасом;
    # загрузке файлов — отдельный лимит (см. MAX_UPLOAD_BYTES в
    # api/v1/schemas/material.py и middleware).
    # Redis для счётчиков лимитов (и очередей). Пусто — счётчики в памяти
    # процесса: годится для разработки, но не для нескольких воркеров.
    redis_url: SecretStr | None = None

    max_body_bytes: int = Field(default=1024 * 1024, gt=0)
    max_upload_bytes: int = Field(default=25 * 1024 * 1024, gt=0)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def cors_origins(self) -> list[str]:
        return _split_csv(self.cors_allowed_origins)

    @property
    def hosts(self) -> list[str]:
        return _split_csv(self.allowed_hosts)

    @model_validator(mode="after")
    def validate_production(self) -> Self:
        # В production небезопасные дефолты — ошибка старта, а не
        # предупреждение в логе, которое никто не прочтёт.
        if self.is_production:
            if "*" in self.hosts:
                raise ValueError("ALLOWED_HOSTS must be explicit in production")
            if "*" in self.cors_origins:
                raise ValueError("CORS_ALLOWED_ORIGINS must not be * in production")
            if self.redis_url is None:
                raise ValueError("REDIS_URL is required in production")
        return self


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@lru_cache
def get_http_settings() -> HttpSettings:
    return HttpSettings()
