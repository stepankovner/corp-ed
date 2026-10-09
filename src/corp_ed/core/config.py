import os
import re
from functools import lru_cache
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from corp_ed.domain.rerank import RERANK_MAX_WORDS

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
    # «Запомнить это устройство» (ТЗ §3): 30 дней. Без галочки — сессия
    # до закрытия браузера, но не дольше короткого срока ниже.
    refresh_token_ttl_days: int = Field(default=30, gt=0, le=90)
    session_refresh_ttl_hours: int = Field(default=12, gt=0, le=72)

    # База данных
    database_url: str

    # Сколько дней хранить журнал вопросов (qa_log). Вопросы сотрудников
    # могут содержать персональные данные даже после маскирования —
    # хранить их дольше, чем нужно отчёту о пробелах, незачем (152-ФЗ).
    qa_log_retention_days: int = Field(default=90, gt=0, le=365)

    # Сколько дней живёт ссылка «поделиться диалогом» (решение владельца
    # 09.10: 30). Продлить — ещё столько же от сегодня, токен прежний.
    chat_share_ttl_days: int = Field(default=30, gt=0, le=365)

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

    # Пустые значения допустимы только в режиме fake (проверка ниже).
    yc_folder_id: str = ""
    # SecretStr: ключ не попадёт в repr настроек и в лог.
    yc_api_key: SecretStr = SecretStr("")
    environment: str = "development"

    # yandex-openai — /v1/chat/completions (все модели каталога, в том числе
    # Flash); yandex-native — /foundationModels/v1/completion (только
    # YandexGPT, запасной вариант по досье 9.2); fake — разработка фронта
    # и сквозные тесты без ключей: ответ собирается из первой выдержки,
    # эмбеддинги — «мешок слов». В production запрещён.
    llm_provider: Literal["yandex-openai", "yandex-native", "fake"] = "yandex-openai"
    llm_model: str = "aliceai-llm-flash"
    # Квота генерации — 10 одновременных запросов на каталог. Лимит — на
    # процесс: при N воркерах uvicorn ставить не больше 10 / N.
    llm_max_concurrency: int = Field(default=8, gt=0, le=10)
    # Режим fake печатает ответ по словам с этой паузой (ТЗ §6): видно,
    # как ответ появляется, и его можно остановить в сквозных тестах.
    llm_fake_stream_delay_ms: int = Field(default=30, ge=0, le=1000)
    # Нагрузочная проверка (docs/LOAD-TEST.md) в режиме fake. Задержка — до
    # первого слова потока и вся длительность ответа без потока
    # (переписывание вопроса, /faq/ask). LLM_FAKE_QUOTAS — те же лимиты, что
    # у настоящей модели: семафор генерации (LLM_MAX_CONCURRENCY) и темп
    # эмбеддингов (EMBEDDING_QUERY_RPS, EMBEDDING_INGEST_RPS). По умолчанию
    # выключено: сквозные тесты и разработка — без ожиданий.
    llm_fake_latency_ms: int = Field(default=0, ge=0, le=60000)
    llm_fake_quotas: bool = False

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
    def validate_provider(self) -> Self:
        if self.llm_provider == "fake":
            if self.environment == "production":
                raise ValueError("LLM_PROVIDER=fake is for development only")
            return self
        if not self.yc_folder_id or not self.yc_api_key.get_secret_value():
            raise ValueError("YC_FOLDER_ID and YC_API_KEY are required")
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
    # Порог «отвечать ли по документам» отдельно от отсечения выдержек
    # (BH-37, domain/threshold.py). Пусто — один порог faq_max_distance,
    # как раньше; включение — решение Артёма (Р-17) после финального
    # прогона, кандидат ML — 0.70.
    faq_gate_distance: float | None = Field(default=None, gt=0, le=2)
    # Ближайший фрагмент дальше faq_max_distance, но не дальше gate — в
    # модель идут выдержки не дальше него на столько (BH-37).
    faq_near_margin: float = Field(default=0.05, ge=0, le=1)
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

    # Память диалога (BH-28): сколько последних реплик видит переписывание
    # вопроса и модель ответа. 3 — замер ML 01.10 на 30 диалогах: верных
    # ответов на уточняющие вопросы 8 → 15 из 19 (p = 0,032), смена темы не
    # портится, уточнение — 1 кредит. 0 — функция выключена целиком.
    history_turns: int = Field(default=3, ge=0, le=10)
    # Диалог живёт в Redis столько после последнего вопроса (решение
    # Артёма 30.09: 12 ч — уточнение после обеда работает; в контракте
    # ML было 30 мин).
    history_ttl_minutes: int = Field(default=720, gt=0, le=7 * 24 * 60)
    # Переписывание вопроса — короткий вызов; ждать его дольше, чем
    # сам ответ, нельзя. Не успел — ответ по исходному вопросу.
    condense_timeout_seconds: float = Field(default=5.0, gt=0, le=30)

    # Реранкер (M3, BH-32; решение Артёма 01.10 — в MVP за флагом, по
    # умолчанию выключен). Пусто — выключен, порядок вектора; имя модели —
    # включён (ML: cross-encoder/mmarco-mMiniLMv2-L12-H384-v1). Имя же
    # уходит в журнал ответов. Модель — отдельный сервис с контрактом
    # text-embeddings-inference /rerank (compose.yaml, профиль reranker);
    # файлы — deploy/reranker/fetch-model.sh. Включение — по итогам
    # holdout 11–12.10.
    rerank_model: str = ""
    rerank_url: str = "http://reranker:8080"
    # Сколько кандидатов вектора отдать модели (замерено при 30; 20 —
    # быстрее, качество не мерили). Не больше --max-client-batch-size
    # сервиса (64 в compose.yaml): больше он не примет.
    rerank_depth: int = Field(default=30, ge=1, le=64)
    # Окно модели в токенах. Сервис режет пары по окну самой модели — 512
    # у mMiniLM, как в замере ML; другого он не умеет, поэтому значение
    # одно. Настройка — чтобы окружение совпадало со стендом ML (BH-32).
    rerank_max_length: int = Field(default=512, ge=512, le=512)
    # Не успел — ответ по порядку вектора. 30 кандидатов на 4 vCPU —
    # около 1,9 с (замер 30.09); 3 с — по контракту BH-32.
    rerank_timeout_ms: int = Field(default=3000, ge=100, le=30_000)
    # BH-40: на вопросах длиннее стольких слов реранкер не зовётся — там он
    # выталкивает нужный фрагмент из пятёрки (замер ML 04.10); прирост
    # подтверждён до 24 слов. Пусто — без ограничения.
    rerank_max_words: int | None = Field(default=RERANK_MAX_WORDS, gt=0)

    model_config = SettingsConfigDict(
        env_prefix="RAG_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def answer_distance(self) -> float:
        """Порог «отвечать ли по документам» по ближайшему фрагменту: gate
        (BH-37), без него — faq_max_distance. Отчёт о пробелах делит по
        нему отказ модели и промах поиска."""
        if self.faq_gate_distance is not None:
            return self.faq_gate_distance
        return self.faq_max_distance

    @field_validator("faq_gate_distance", "rerank_max_words", mode="before")
    @classmethod
    def empty_gate_is_off(cls, value: object) -> object:
        # RAG_FAQ_GATE_DISTANCE= или RAG_RERANK_MAX_WORDS= в .env —
        # выключено, а не ошибка числа.
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def validate_overlap(self) -> Self:
        # split_document сам кидает ValueError, но на первом ингесте.
        # Падать на старте дешевле, чем узнать об ошибке от клиента.
        if self.overlap_tokens >= self.chunk_tokens:
            raise ValueError("overlap_tokens must be less than chunk_tokens")
        # Gate ближе порога выдержек ничего бы не менял — это опечатка.
        if (
            self.faq_gate_distance is not None
            and self.faq_gate_distance < self.faq_max_distance
        ):
            raise ValueError("faq_gate_distance must not be less than faq_max_distance")
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
    исчерпании. 420 кредитов на место в месяц (20 обращений × 21 день,
    досье v3.3). 1 кредит = 4 000 токенов ≈ одно обращение, в том числе
    уточняющее (решение Артёма 29.09, BH-30): при 2 000 вопрос с медианой
    1 844 токена округлялся до 2 кредитов в 13 из 33 случаев — выходило
    14–15 вопросов в день вместо 20 (ROADMAP.md, «Р-4 подробно»).
    """

    credits_per_seat: int = Field(default=420, gt=0)
    tokens_per_credit: int = Field(default=4000, gt=0)
    # Месяц считается по московскому времени: клиенты и счета — в России.
    billing_timezone: str = "Europe/Moscow"
    warn_at_percent: int = Field(default=80, gt=0, lt=100)
    # Цена 1 000 токенов модели ответа в рублях — для оценки расхода в
    # нашей панели (ТЗ §9). Не задана — панель показывает только токены.
    llm_rub_per_1k_tokens: float | None = Field(default=None, ge=0)

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


class LeadSettings(BaseSettings):
    """Заявки на созвон со страницы тарифов (досье 3.3 и 10.1, решение 28.09).

    Форма собирает имя и телефон — это персональные данные, а мы их
    оператор: до политики обработки и согласия в форме (досье 17.1) приём
    выключен. Включение — LEADS_ENABLED=true вместе с адресом политики и
    её версией: согласие в заявке записывается с версией, на которую
    человек согласился.
    """

    enabled: bool = False
    policy_url: str = ""
    policy_version: str = Field(default="", max_length=64)
    # Сколько хранить заявку: созвон состоялся или нет — через полгода
    # данные не нужны. Удаляет `cli purge`.
    retention_days: int = Field(default=180, gt=0, le=730)
    # Выбор даты созвона: с завтрашнего дня на столько дней вперёд.
    days_ahead: int = Field(default=30, gt=0, le=90)
    # Ящик команды для писем о новой заявке — с контактами, чтобы
    # перезвонить без cli. Почта уходит через наш почтовый сервис
    # (MAIL_BACKEND), а не в чужой мессенджер: в Telegram контактов нет.
    # Пусто — писем нет.
    notify_email: str = Field(default="", max_length=254)

    model_config = SettingsConfigDict(
        env_prefix="LEADS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("policy_url")
    @classmethod
    def validate_policy_url(cls, value: str) -> str:
        value = value.strip()
        if value and not (value.startswith("https://") or value.startswith("/")):
            raise ValueError("LEADS_POLICY_URL: https://… или путь на этом сайте")
        return value

    @field_validator("notify_email")
    @classmethod
    def validate_notify_email(cls, value: str) -> str:
        value = value.strip()
        if value and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError(
                "LEADS_NOTIFY_EMAIL: адрес почты, например info@krontoai.ru"
            )
        return value

    @model_validator(mode="after")
    def validate_policy_for_enabled(self) -> Self:
        # Форма без политики и согласия — нарушение 152-ФЗ, а не мелочь:
        # такой конфиг не должен стартовать.
        if self.enabled and not (self.policy_url and self.policy_version):
            raise ValueError(
                "LEADS_ENABLED requires LEADS_POLICY_URL and LEADS_POLICY_VERSION"
            )
        return self


@lru_cache
def get_lead_settings() -> LeadSettings:
    return LeadSettings()


class DemoSettings(BaseSettings):
    """Песочница на сайте (ТЗ §1): вопросы без входа к вымышленной
    компании (corp_ed/demo). Компанию заводит и обновляет `cli demo
    setup` — его запускает выкатка; пока её нет, песочница отвечает
    «недоступна». Вопрос стоит вызова модели, поэтому лимиты строгие и
    при недоступном Redis — отказ (api/v1/rate_limits.py, DEMO_*).
    """

    enabled: bool = True
    company_code: str = Field(default="demo-site", pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    # Учётка, от имени которой песочница спрашивает. Войти в неё нельзя:
    # вместо хеша пароля — заглушка, которую не примет ни один пароль.
    account_email: str = "demo@krontoai.ru"
    # Места компании — её пул кредитов на месяц: потолок расходов на модель.
    seats: int = Field(default=30, gt=0, le=1000)

    model_config = SettingsConfigDict(
        env_prefix="DEMO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_demo_settings() -> DemoSettings:
    return DemoSettings()


class TeamNotifySettings(BaseSettings):
    """Бот в Telegram для нашей команды (решение 28.09, П-5).

    Без токена и чата уведомлений нет. Сообщения — без персональных
    данных (services/team_notify.py). Токен — секрет: в логи не пишется.
    """

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = Field(default=None, max_length=64)

    model_config = SettingsConfigDict(
        env_prefix="TEAM_NOTIFY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_pair(self) -> Self:
        # Половина пары — почти наверняка забытая переменная: молча
        # остаться без уведомлений хуже, чем не стартовать.
        if (self.telegram_bot_token is None) != (self.telegram_chat_id is None):
            raise ValueError(
                "set both TEAM_NOTIFY_TELEGRAM_BOT_TOKEN "
                "and TEAM_NOTIFY_TELEGRAM_CHAT_ID"
            )
        return self


@lru_cache
def get_team_notify_settings() -> TeamNotifySettings:
    return TeamNotifySettings()


class MailSettings(BaseSettings):
    """Отправка писем (ТЗ §3, решение 03.10).

    backend:
    - postbox — Yandex Cloud Postbox по HTTPS (порт 443), статический ключ
      сервисного аккаунта с ролью postbox.sender. Стенд и бой: Selectel
      закрывает исходящие 25, 465 и 587 (RISKS №57), SMTP оттуда не
      доходит ни до одного почтового сервера;
    - smtp — отправка по SMTP (сервер без закрытых портов; CI — Mailpit);
    - console — письмо в лог вместо отправки (разработка);
    - memory — в список в памяти процесса (тесты).

    Стенд — STAGE.md §4.5а; сквозные проверки CI — перехватчик писем
    Mailpit (ci.yaml, задание e2e): наружу ничего не уходит. Пароль и
    ключ — секреты: в логи не пишутся.
    """

    backend: Literal["postbox", "smtp", "console", "memory"] = "console"
    # Общая ENVIRONMENT, не MAIL_ENVIRONMENT: префикс к ней не относится.
    # В production console и memory не отправляют писем — воркер не
    # стартует с ними (mail.build_sender).
    environment: str = Field(
        default="development",
        validation_alias=AliasChoices("environment", "ENVIRONMENT"),
    )
    smtp_host: str | None = None
    smtp_port: int = Field(default=465, gt=0, lt=65536)
    smtp_security: Literal["ssl", "starttls", "none"] = "ssl"
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    # Таймаут и SMTP, и запроса к Postbox.
    smtp_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    postbox_key_id: str | None = None
    postbox_secret_key: SecretStr | None = None
    postbox_url: str = "https://postbox.cloud.yandex.net"
    postbox_region: str = "ru-central1"
    from_address: str = "noreply@krontoai.ru"
    from_name: str = "kronto"
    # Адрес сайта для ссылок в письмах, без «/» в конце.
    site_url: str = "http://localhost:5173"

    model_config = SettingsConfigDict(
        env_prefix="MAIL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("site_url")
    @classmethod
    def strip_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_smtp(self) -> Self:
        # Без хоста smtp молча не отправит ни одного письма — регистрация
        # встанет на подтверждении почты. Лучше не стартовать.
        if self.backend == "smtp" and not self.smtp_host:
            raise ValueError("MAIL_SMTP_HOST is required for MAIL_BACKEND=smtp")
        if self.backend == "postbox" and not (
            self.postbox_key_id and self.postbox_secret_key
        ):
            raise ValueError(
                "MAIL_POSTBOX_KEY_ID and MAIL_POSTBOX_SECRET_KEY are required "
                "for MAIL_BACKEND=postbox"
            )
        return self


@lru_cache
def get_mail_settings() -> MailSettings:
    return MailSettings()


class RegistrationSettings(BaseSettings):
    """Самостоятельная регистрация (ТЗ §2, §11).

    enabled=false — регистрироваться можно только по приглашению: так
    на боевом домене, пока нет юридических текстов от ИП. policy_version
    — редакция политики обработки ПДн, на которую человек дал согласие
    (пишется в учётку).
    """

    enabled: bool = True
    policy_url: str = "/privacy"
    policy_version: str = Field(default="draft-2026-10-04", max_length=64)

    model_config = SettingsConfigDict(
        env_prefix="REGISTRATION_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_registration_settings() -> RegistrationSettings:
    return RegistrationSettings()


class AuthSettings(BaseSettings):
    """Ключи доступа (WebAuthn, ТЗ §3).

    Пусто — сайт берётся из заголовка Host (его проверяет TrustedHost):
    https://<хост>, для localhost — http. Задавать нужно, только если
    фронт и API на разных адресах (разработка: http://localhost:5173).
    """

    webauthn_rp_id: str | None = None
    # Через запятую: http://localhost:5173,https://stage.krontoai.ru
    webauthn_origins: str = ""

    model_config = SettingsConfigDict(
        env_prefix="AUTH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def origins(self) -> list[str]:
        return _split_csv(self.webauthn_origins)


@lru_cache
def get_auth_settings() -> AuthSettings:
    return AuthSettings()


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


EXTRA_FORMAT_NAMES = ("xlsx", "pptx", "doc")
"""Форматы Р-5, которые включает INGEST_EXTRA_FORMATS (ingest/extract.py)."""


class IngestSettings(BaseSettings):
    """Разбор файлов в песочнице (API — загрузка, воркер — коннекторы).

    extra_formats — форматы Р-5 (решение Артёма 29.09: по одному, после
    приёмки ML; .xlsx, .pptx и .doc приняты 01.10, ml-formats.md), через
    запятую. Убрать формат — он снова отклоняется с подсказкой и не
    скачивается из подключённых систем; уже загруженные файлы остаются.
    """

    extra_formats: str = "xlsx,pptx,doc"

    model_config = SettingsConfigDict(
        env_prefix="INGEST_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("extra_formats")
    @classmethod
    def validate_extra_formats(cls, value: str) -> str:
        # Опечатка в имени формата — ошибка старта, а не молча выключенный
        # формат.
        unknown = set(_split_csv(value.lower())) - set(EXTRA_FORMAT_NAMES)
        if unknown:
            raise ValueError(
                f"INGEST_EXTRA_FORMATS: unknown {sorted(unknown)}, "
                f"allowed {list(EXTRA_FORMAT_NAMES)}"
            )
        return value

    @property
    def extra_format_names(self) -> list[str]:
        return _split_csv(self.extra_formats.lower())


@lru_cache
def get_ingest_settings() -> IngestSettings:
    return IngestSettings()


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

    # Общая ENVIRONMENT, не CONNECTOR_ENVIRONMENT: префикс к ней не
    # относится, иначе боевые проверки ниже на стенде не включались бы.
    environment: str = Field(
        default="development",
        validation_alias=AliasChoices("environment", "ENVIRONMENT"),
    )
    secrets_keys: SecretStr | None = None
    max_per_tenant: int = Field(default=20, gt=0, le=1000)
    default_sync_interval_minutes: int = Field(default=60, ge=15, le=1440)
    # Бюджет одного запуска: документов и минут; остаток — следующим.
    max_documents_per_run: int = Field(default=200, gt=0, le=5000)
    max_run_minutes: int = Field(default=20, gt=0, le=180)
    # Больше — не скачивается: тот же порядок, что у ручной загрузки.
    max_document_bytes: int = Field(default=25 * 1024 * 1024, gt=0)
    # Срок на скачивание одного файла целиком (core/outbound.py): таймаут
    # запроса — на каждое чтение, и медленная отдача его не исчерпает.
    download_timeout_seconds: int = Field(default=300, gt=0, le=3600)
    sync_run_retention_days: int = Field(default=90, gt=0, le=365)
    # Процесс за egress-прокси (HTTPS_PROXY): запросы к системам клиентов
    # уходят по имени хоста, а не на закреплённый IP — прокси отвергает
    # CONNECT к IP и сам резолвит имя. Проверка адреса (публичный IP, без
    # учётных данных в URL) остаётся; закрепление против DNS rebinding —
    # на политике прокси (core/outbound.py, DEPLOY.md §9a).
    outbound_via_proxy: bool = False
    # Модули, не проверенные на живой системе, скрыты из каталога и не
    # принимаются при создании подключения, пока не перечислены здесь
    # (через запятую). Для живой проверки `cli connector-check` — тот же
    # флаг. Решение 28.09: «База знаний 2.0» Битрикс24 — до проверки на
    # портале (RISKS №36).
    preview_modules: str = ""
    # Виды подключений целиком (bitrix24, confluence, yandex360 — через
    # запятую), которые компаниям не предлагаются: их нет в каталоге,
    # новое подключение не создать. Уже созданные работают как раньше:
    # синхронизация, вход сотрудников, настройки, удаление;
    # `cli connector-check` их тоже видит — для живой проверки. Для
    # решения «не проверили на живой системе к MVP — скрыть» (STATUS.md).
    hidden_kinds: str = ""

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
    yandex_wiki_api: str = "https://api.wiki.yandex.net/"

    model_config = SettingsConfigDict(
        env_prefix="CONNECTOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_production(self) -> Self:
        if self.environment != "production":
            # В разработке фронт живёт на http://localhost — адрес возврата
            # и подменные серверы стенда могут быть без TLS.
            return self
        if self.secrets_keys is None:
            raise ValueError("CONNECTOR_SECRETS_KEYS is required in production")
        for name in (
            "oauth_callback_url",
            "oauth_return_url",
            "bitrix24_oauth_server",
            "yandex_oauth_server",
            "yandex_disk_api",
            "yandex_wiki_api",
        ):
            value = getattr(self, name)
            # Браузер сотрудника и секрет приложения ходят по этим адресам:
            # http здесь — утечка кода авторизации или секрета в открытую.
            if value is not None and not value.startswith("https://"):
                raise ValueError(f"CONNECTOR_{name.upper()} must be an https:// URL")
        if self.outbound_via_proxy and not (
            os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        ):
            # Без прокси флаг просто снимает закрепление адреса — защиту
            # от DNS rebinding (RISKS №39).
            raise ValueError(
                "CONNECTOR_OUTBOUND_VIA_PROXY requires HTTPS_PROXY in production"
            )
        return self

    @property
    def keys(self) -> list[str]:
        if self.secrets_keys is None:
            return []
        return _split_csv(self.secrets_keys.get_secret_value())

    @property
    def enabled_preview_modules(self) -> frozenset[str]:
        return frozenset(_split_csv(self.preview_modules))

    @property
    def hidden_kind_names(self) -> frozenset[str]:
        return frozenset(_split_csv(self.hidden_kinds))


@lru_cache
def get_connector_settings() -> ConnectorSettings:
    return ConnectorSettings()
