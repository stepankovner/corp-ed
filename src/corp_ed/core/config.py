from functools import lru_cache
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

EMBEDDING_DIM = 768
"""Размерность эмбеддингов — свойство СХЕМЫ базы (chunks.embedding
vector(768)), а не только настройка. text-embeddings-v2 с dim=768 —
решение ML от 25.09 (MRR +0,048 к 256, p = 0,003). Сменить её можно
только миграцией колонки и полной переиндексацией: векторы разных
размерностей и моделей несравнимы."""

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

    # Сколько дней хранить журнал вопросов (qa_log). Вопросы сотрудников
    # могут содержать персональные данные даже после маскирования —
    # хранить их дольше, чем нужно отчёту о пробелах, незачем (152-ФЗ).
    qa_log_retention_days: int = Field(default=90, gt=0, le=365)

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

    # Эмбеддинги — пара моделей <семейство>-doc / <семейство>-query.
    # Смена семейства без переиндексации смешает в выдаче векторы двух
    # моделей: расстояния несравнимы, порог отказа теряет смысл.
    embedding_model: Literal["text-embeddings-v2", "text-search"] = "text-embeddings-v2"
    embedding_dim: int = EMBEDDING_DIM

    # Эмбеддинги: квота 10 запросов в секунду на каталог, общая для API и
    # воркера. Вопросам сотрудников — своя доля, ингесту — своя, в сумме
    # с запасом до квоты (BH-4: «поиск приоритетнее ингеста»).
    embedding_query_rps: float = Field(default=3.0, gt=0)
    embedding_ingest_rps: float = Field(default=6.0, gt=0)

    @model_validator(mode="after")
    def validate_embedding_dim(self) -> Self:
        # Лучше не стартовать, чем писать векторы, которые не лягут в
        # колонку, или — хуже — искать ими по чужой размерности.
        if self.embedding_dim != EMBEDDING_DIM:
            raise ValueError(
                f"EMBEDDING_DIM={self.embedding_dim} does not match the schema "
                f"({EMBEDDING_DIM}); changing it needs a migration and a reindex"
            )
        if self.embedding_model == "text-search" and self.embedding_dim != 256:
            raise ValueError("text-search produces 256-dimensional vectors only")
        return self

    @model_validator(mode="after")
    def validate_embedding_quota(self) -> Self:
        if self.embedding_query_rps + self.embedding_ingest_rps > 10:
            raise ValueError("embedding rps shares exceed the folder quota of 10")
        return self

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
    # Способ поиска (M1, BH-12): vector или hybrid. Переключает ML по
    # замеру на золотом наборе, поэтому тоже без дефолта.
    retriever: Literal["vector", "hybrid"]
    # Вес полнотекстовой ветки в RRF (вектор — 1.0). Стартовое 0.5 — ТЗ
    # и оптимум Битрикс24; вес выше 1.0 у них ухудшал качество.
    fulltext_weight: float = Field(gt=0, le=2)

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


class GapsSettings(BaseSettings):
    """Ночной отчёт о пробелах (BH-21).

    cluster_distance и half_life_days — значения ML (задача 3), без
    дефолтов, как у RAG_*. Пороги полнотекста для classify_miss ML
    просит подобрать заново на живых логах (ts_rank_cd, а не доля слов,
    как в замере): пока они не заданы, полнотекст в классификации не
    участвует — пробелом считается всё, где вектор не прошёл порог.
    Окно и потолки — инженерные ограничения бэкенда, с дефолтами.
    """

    cluster_distance: float = Field(gt=0, lt=2)
    half_life_days: float = Field(gt=0)
    strong_fulltext: float | None = Field(default=None, ge=0)
    empty_fulltext: float | None = Field(default=None, ge=0)
    off_topic_distance: float | None = Field(default=None, gt=0, le=2)
    window_days: int = Field(default=30, gt=0, le=90)
    # Кластеризация ML — O(n³): 2 000 вопросов — ~9 с, 4 000 — больше минуты.
    max_questions: int = Field(default=2000, gt=0, le=5000)
    # Один вопрос — ещё не тема; в отчёт идут группы от двух вопросов.
    min_cluster_size: int = Field(default=2, gt=0)
    # Подписей за ночь на компанию: ~0,05 ₽ каждая, остальные — завтра.
    max_labels_per_run: int = Field(default=50, gt=0)

    model_config = SettingsConfigDict(
        env_prefix="GAPS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_fulltext_thresholds(self) -> Self:
        # Половина пары — это почти наверняка забытая переменная.
        if (self.strong_fulltext is None) != (self.empty_fulltext is None):
            raise ValueError("set both GAPS_STRONG_FULLTEXT and GAPS_EMPTY_FULLTEXT")
        if (
            self.strong_fulltext is not None
            and self.empty_fulltext is not None
            and self.empty_fulltext > self.strong_fulltext
        ):
            raise ValueError("GAPS_EMPTY_FULLTEXT must not exceed GAPS_STRONG_FULLTEXT")
        return self


class BillingSettings(BaseSettings):
    """Пул кредитов компании (досье 10.2).

    Структура решена командой 24.09: один пул на компанию, один тип
    кредита, персональных лимитов нет, жёсткая остановка при
    исчерпании. Числа — ПРЕДЛОЖЕНИЕ досье, не утверждены: 1 кредит ≈
    одно обычное обращение (~2 000 токенов), 420 кредитов на место в
    месяц (20 обращений × 21 день). Поэтому — настройки с дефолтами.
    """

    credits_per_seat: int = Field(default=420, gt=0)
    tokens_per_credit: int = Field(default=2000, gt=0)
    # Месяц считается по московскому времени: клиенты и счета — в России.
    billing_timezone: str = "Europe/Moscow"
    warn_at_percent: int = Field(default=80, gt=0, lt=100)

    model_config = SettingsConfigDict(
        env_prefix="BILLING_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("billing_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        # Опечатка в поясе должна ронять старт, а не первый вопрос месяца.
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unknown BILLING_TIMEZONE: {value}") from exc
        return value

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.billing_timezone)


@lru_cache
def get_billing_settings() -> BillingSettings:
    return BillingSettings()


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


class ConnectorSettings(BaseSettings):
    """Коннекторы к источникам документов (досье 10.5, DECISIONS «Коннекторы»).

    secrets_keys — ключи Fernet через запятую, первым — действующий:
    им шифруются учётные данные источников в базе; остальные — для
    расшифровки во время ротации (docs/DEPLOY.md). Без ключа коннекторы
    недоступны (503 на записи секретов); в production ключ обязателен
    на старте. Сгенерировать:
    python -c "from cryptography.fernet import Fernet; \
    print(Fernet.generate_key().decode())"

    max_per_tenant — технический потолок подключений на компанию (защита
    от скрипта), не тарифная граница: тариф строится от мест (решение
    команды 25.09).
    """

    environment: str = "development"
    secrets_keys: SecretStr | None = None
    max_per_tenant: int = Field(default=20, gt=0, le=1000)
    default_sync_interval_minutes: int = Field(default=60, ge=15, le=1440)
    # Бюджет одного запуска: документов и минут; остаток — следующим.
    max_documents_per_run: int = Field(default=200, gt=0, le=5000)
    max_run_minutes: int = Field(default=20, gt=0, le=180)
    # Больше — не скачивается: тот же порядок, что у ручной загрузки.
    max_document_bytes: int = Field(default=25 * 1024 * 1024, gt=0)
    sync_run_retention_days: int = Field(default=90, gt=0, le=365)

    # OAuth-приложения (режим per_user, этап 2). callback — публичный
    # адрес ручки GET /api/v1/connectors/oauth/callback: его админ
    # вписывает в карточку приложения на портале, поэтому он показывается
    # в каталоге видов. return — страница фронта, куда возвращается
    # браузер сотрудника после обмена кода; без неё ручка отвечает JSON
    # (стенд, curl). state живёт oauth_state_ttl_minutes: дольше — окно
    # для повторного использования перехваченного редиректа.
    oauth_callback_url: str | None = None
    oauth_return_url: str | None = None
    oauth_state_ttl_minutes: int = Field(default=10, gt=0, le=60)
    # Сервер авторизации Битрикс24 — один на облако и коробку; в
    # документации 2026 года — oauth.bitrix24.tech (раньше oauth.bitrix.info).
    bitrix24_oauth_server: str = "https://oauth.bitrix24.tech/"
    # Яндекс: OAuth Яндекс ID и REST Диска — фиксированные адреса, не
    # адрес клиента; настройки — чтобы тесты и стенд могли их подменить.
    yandex_oauth_server: str = "https://oauth.yandex.ru/"
    yandex_disk_api: str = "https://cloud-api.yandex.net/"

    model_config = SettingsConfigDict(
        env_prefix="CONNECTOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_production(self) -> Self:
        if self.environment == "production" and self.secrets_keys is None:
            raise ValueError("CONNECTOR_SECRETS_KEYS is required in production")
        for name in (
            "oauth_callback_url",
            "oauth_return_url",
            "bitrix24_oauth_server",
            "yandex_oauth_server",
            "yandex_disk_api",
        ):
            value = getattr(self, name)
            # Браузер сотрудника и секрет приложения ходят по этим адресам:
            # http здесь — утечка кода авторизации или секрета в открытую.
            if value is not None and not value.startswith("https://"):
                raise ValueError(f"CONNECTOR_{name.upper()} must be an https:// URL")
        return self

    @property
    def keys(self) -> list[str]:
        if self.secrets_keys is None:
            return []
        return _split_csv(self.secrets_keys.get_secret_value())


@lru_cache
def get_connector_settings() -> ConnectorSettings:
    return ConnectorSettings()
