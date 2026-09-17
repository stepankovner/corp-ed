from functools import lru_cache
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Конфигурация приложения. Значения читаются из переменных окружения / .env."""

    # Окружение
    environment: str = "development"
    debug: bool = False
    secret_key: str

    # База данных
    database_url: str

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


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
    """Параметры нарезки и поиска.

    Значения — зона ML (Артём). Здесь только проводка: дефолтов нет
    намеренно, чтобы придуманные числа не стали продакшен-значениями.
    """

    chunk_size: int = Field(gt=0)
    chunk_overlap: int = Field(ge=0)
    faq_limit: int = Field(gt=0)
    faq_max_distance: float = Field(gt=0, le=2)

    model_config = SettingsConfigDict(
        env_prefix="RAG_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_chunk_overlap(self) -> Self:
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be less than chunk_size")
        return self


class CorsSettings(BaseSettings):
    """Origin'ы, которым браузер разрешит обращаться к API.

    Отдельный класс, а не поле в Settings: main.py читает эти настройки
    при сборке приложения, а Settings требует секретов — импорт
    приложения перестал бы работать без .env, и падали бы тесты
    и alembic. Здесь у каждого поля есть дефолт, поэтому импорт
    не зависит от окружения.

    Дефолт — адрес dev-сервера Vite, он годится только для локальной
    разработки. В бою origin задаётся переменной окружения.
    """

    cors_allowed_origins: str = "http://localhost:5173"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def allowed_origins(self) -> list[str]:
        """Origin'ы списком.

        Хранится строкой через запятую, а не list[str]: сложные типы
        pydantic-settings разбирает как JSON, и переменная окружения
        превратилась бы в ["http://..."] — лишний источник опечаток
        при деплое.
        """
        return [
            origin.strip()
            for origin in self.cors_allowed_origins.split(",")
            if origin.strip()
        ]


@lru_cache
def get_cors_settings() -> CorsSettings:
    return CorsSettings()
