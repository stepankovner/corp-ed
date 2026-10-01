import enum
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, deferred, mapped_column

from corp_ed.core.config import EMBEDDING_DIM
from corp_ed.core.database import Base
from corp_ed.domain.mixins import TenantMixin
from corp_ed.domain.tariffs import DEFAULT_TARIFF
from corp_ed.domain.types import DEFAULT_NOT_FOUND_MODE


class UserRole(enum.Enum):
    """Роль сотрудника внутри своей компании.

    ADMIN — управляет документами и пользователями компании, видит
    отладку поиска и отчёт о пробелах. EMPLOYEE — задаёт вопросы.
    Заводить компании (тенанты) не может ни одна роль: это делает
    команда Kronto через CLI на сервере (см. corp_ed.cli).
    """

    ADMIN = "admin"
    EMPLOYEE = "employee"


class MaterialStatus(enum.Enum):
    """Где материал на пути к поиску.

    PENDING — поставлен в очередь, PROCESSING — воркер нарезает и считает
    эмбеддинги, READY — чанки на месте, FAILED — не получилось (причина —
    кодом в status_error, подробности только в логе).
    """

    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class IngestJobStatus(enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Tenant(Base):
    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint("seats > 0", name="ck_tenants_seats_positive"),
        CheckConstraint(
            "not_found_mode IN ('general', 'strict')",
            name="ck_tenants_not_found_mode",
        ),
        CheckConstraint(
            "tariff IN ('base', 'extended', 'enterprise')",
            name="ck_tenants_tariff",
        ),
        CheckConstraint(
            "connector_limit IS NULL OR connector_limit > 0",
            name="ck_tenants_connector_limit_positive",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    company_code: Mapped[str] = mapped_column(unique=True, index=True)
    name: Mapped[str]
    # Приостановленная компания (неоплата, окончание пилота, инцидент):
    # вход закрыт, выданные токены перестают приниматься.
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
    # Оплаченные места. Пул кредитов на месяц = места × кредитов на место
    # (досье 10.2). Задаёт команда при подключении (CLI).
    seats: Mapped[int] = mapped_column(default=30, server_default="30")
    # Ответ, когда в документах ничего нет: strict или general
    # (NotFoundMode). По умолчанию general (DEFAULT_NOT_FOUND_MODE);
    # меняет команда через CLI.
    not_found_mode: Mapped[str] = mapped_column(
        String(16),
        default=DEFAULT_NOT_FOUND_MODE.value,
        server_default=DEFAULT_NOT_FOUND_MODE.value,
    )
    # Тариф (domain/tariffs.py, решение 30.09): сколько подключений и
    # какие системы. Задаёт команда через CLI.
    tariff: Mapped[str] = mapped_column(
        String(16), default=DEFAULT_TARIFF.value, server_default=DEFAULT_TARIFF.value
    )
    # Технический потолок подключений для этой компании; NULL — общий
    # CONNECTOR_MAX_PER_TENANT. Поднимает команда (cli set-tariff).
    connector_limit: Mapped[int | None]


class User(TenantMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_user_tenant_email"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    email: Mapped[str]
    hashed_password: Mapped[str]
    full_name: Mapped[str | None]
    role: Mapped[UserRole]
    is_active: Mapped[bool] = mapped_column(default=True)
    # Версия токенов: увеличивается при смене пароля, роли, блокировке и
    # выходе со всех устройств. Access-токен со старой версией отвергается.
    token_version: Mapped[int] = mapped_column(default=0, server_default="0")
    # Пароль выдан администратором: до смены доступ только к смене пароля.
    must_change_password: Mapped[bool] = mapped_column(
        default=False, server_default=false()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Invite(TenantMixin, Base):
    """Ссылка-приглашение в компанию (решение 28.09).

    Админ отправляет ссылку куда угодно (мессенджер, почта); по ней человек
    сам заводит учётку сотрудника в этой компании. Хранится только sha256
    токена, как у refresh-токенов: утечка таблицы не даёт действующих
    ссылок. Под RLS: ссылка ищется в контексте компании из адреса
    (/join/<код>#<токен>).
    """

    __tablename__ = "invites"
    __table_args__ = (
        CheckConstraint("max_uses > 0", name="ck_invites_max_uses_positive"),
        CheckConstraint("uses >= 0 AND uses <= max_uses", name="ck_invites_uses"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    max_uses: Mapped[int]
    uses: Mapped[int] = mapped_column(default=0, server_default="0")
    # Почта присоединяющегося — только в этом домене (и его поддоменах);
    # пусто — любая. Хранится в нижнем регистре, без «@».
    email_domain: Mapped[str | None] = mapped_column(String(253))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Lead(Base):
    """Заявка на созвон со страницы тарифов (досье 10.1, решение 28.09).

    Не тенантская: клиента ещё нет. Персональные данные (имя, телефон,
    почта) — только то, что нужно, чтобы перезвонить; IP не хранится.
    Согласие записывается с версией политики. Срок хранения —
    LEADS_RETENTION_DAYS, удаляет `cli purge`.
    """

    __tablename__ = "leads"
    __table_args__ = (
        CheckConstraint(
            "status IN ('new', 'contacted', 'scheduled', 'rejected')",
            name="ck_leads_status",
        ),
        CheckConstraint(
            "tariff IN ('base', 'extended', 'enterprise')", name="ck_leads_tariff"
        ),
        CheckConstraint("seats > 0", name="ck_leads_seats_positive"),
        Index("ix_leads_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    company_name: Mapped[str] = mapped_column(String(200))
    contact_name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(254))
    seats: Mapped[int]
    tariff: Mapped[str] = mapped_column(String(16))
    preferred_date: Mapped[date]
    preferred_slot: Mapped[str] = mapped_column(String(16))
    comment: Mapped[str | None] = mapped_column(Text)
    policy_version: Mapped[str] = mapped_column(String(64))
    consented_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="new", server_default="new")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RefreshToken(Base):
    """Выданный refresh-токен (хранится только sha256).

    Не TenantMixin намеренно: токен предъявляют до того, как известен
    тенант, — поиск идёт по хешу, и уже из найденной строки берутся
    пользователь и тенант. tenant_id хранится для аудита и каскадов.

    family_id объединяет цепочку ротаций одного входа. Повторное
    предъявление уже использованного токена — признак кражи: отзывается
    вся семья (RFC 9700, раздел 4.14.2).
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    family_id: Mapped[UUID] = mapped_column(index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Material(TenantMixin, Base):
    __tablename__ = "materials"
    __table_args__ = (
        # Один и тот же файл дважды в одной компании — дубль выдержек в
        # выдаче и двойная цена эмбеддингов. Только для ручных загрузок:
        # один файл на двух дисках коннекторов — два документа с разными
        # ссылками, sha256 у них — признак «не изменился», не ключ.
        Index(
            "uq_materials_tenant_sha256",
            "tenant_id",
            "source_sha256",
            unique=True,
            postgresql_where=text("source_sha256 IS NOT NULL AND connector_id IS NULL"),
        ),
        Index(
            "uq_materials_connector_external_id",
            "connector_id",
            "external_id",
            unique=True,
            postgresql_where=text("connector_id IS NOT NULL"),
        ),
        CheckConstraint(
            "visibility IN ('tenant', 'restricted')", name="ck_materials_visibility"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    title: Mapped[str]
    content: Mapped[str]
    # Исходный файл не хранится (меньше персональных данных у нас) —
    # только извлечённый текст и сведения о файле для справки и дублей.
    # Для текста, вставленного в форму, поля пустые.
    source_filename: Mapped[str | None] = mapped_column(String(255))
    source_format: Mapped[str | None] = mapped_column(String(16))
    source_sha256: Mapped[str | None] = mapped_column(String(64))
    source_size: Mapped[int | None]
    status: Mapped[MaterialStatus] = mapped_column(
        default=MaterialStatus.PENDING, server_default="PENDING"
    )
    # Код причины, а не текст исключения: админ компании видит его в
    # интерфейсе, а трассировка и ответ провайдера — только в логе.
    status_error: Mapped[str | None] = mapped_column(String(64))
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Документ из источника (коннектор): стабильный id в системе,
    # ссылка для сотрудника, версия (etag / дата / ревизия) для
    # сравнения без скачивания. У ручных загрузок всё пусто.
    connector_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("connectors.id", ondelete="CASCADE"), index=True
    )
    external_id: Mapped[str | None] = mapped_column(String(512))
    source_url: Mapped[str | None] = mapped_column(String(2048))
    external_version: Mapped[str | None] = mapped_column(String(128))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # tenant — видят все сотрудники; restricted — только по material_access
    # (MaterialVisibility). Проверяется в поиске чанков.
    visibility: Mapped[str] = mapped_column(
        String(16), default="tenant", server_default="tenant"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MaterialAccess(TenantMixin, Base):
    """Кому виден документ с visibility = restricted.

    Заполняет синхронизация коннектора: в режиме organization — из ACL
    источника по почте сотрудника, в режиме per_user — фактом «документ
    есть в листинге этого сотрудника». Удаление документа или
    сотрудника убирает строки каскадом.
    """

    __tablename__ = "material_access"

    material_id: Mapped[UUID] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class Chunk(TenantMixin, Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("material_id", "position", name="uq_chunk_material_position"),
        Index("ix_chunks_fts", "fts", postgresql_using="gin"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    material_id: Mapped[UUID] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int]
    # Стек заголовков секции без названия документа: ["Раздел 3", "3.2 …"].
    heading_path: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default="{}"
    )
    # Крошки + текст без разметки. По нему считается эмбеддинг; хранится,
    # чтобы из него же строилась полнотекстовая ветка поиска (M1).
    embed_text: Mapped[str] = mapped_column(Text, default="", server_default="")
    # Полнотекстовая ветка гибридного поиска (M1, BH-12). Считает сама
    # база из embed_text — тот же текст, что у эмбеддинга, поэтому ветки
    # видят одно и то же. deferred: в Python вектор лексем не нужен.
    fts: Mapped[str] = deferred(
        mapped_column(
            TSVECTOR,
            Computed("to_tsvector('russian', embed_text)", persisted=True),
        )
    )
    # Крошки + Markdown (llm_text) — то, что уходит в промпт.
    content: Mapped[str]
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    model: Mapped[str]
    model_version: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AuditEvent(Base):
    """Запись журнала аудита: кто, что, над чем, когда, откуда.

    Не TenantMixin намеренно: часть событий происходит до того, как
    тенант известен (неудачный вход с неверным кодом компании), и такие
    записи должны сохраниться. Поэтому tenant_id допускает NULL, а
    выборки для админа фильтруются по нему явно.

    Журнал только дописывается: UPDATE запрещён триггером в базе,
    DELETE — только для записей старше срока хранения (см. миграцию).
    Защита на уровне базы, а не кода: запись о действии не должна
    исчезать вместе с тем, кто это действие совершил.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL")
    )
    actor_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    ip: Mapped[str | None] = mapped_column(String(45))
    request_id: Mapped[str | None] = mapped_column(String(36))
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class IngestJob(Base):
    """Задача фонового ингеста (нарезка + эмбеддинги + замена чанков).

    Очередь — таблица в Postgres: задача ставится в той же транзакции,
    что и материал, поэтому не теряется между базой и брокером и не
    появляется для материала, чья транзакция откатилась.

    Не TenantMixin и не под RLS намеренно: воркер забирает задачи всех
    компаний по очереди и до выбора задачи тенанта не знает. В таблице
    только идентификаторы; содержимое материала воркер читает уже в
    контексте тенанта задачи.
    """

    __tablename__ = "ingest_jobs"
    __table_args__ = (
        # Не больше одной активной задачи на материал: повторный запрос
        # переиндексации не плодит параллельные пересчёты одного документа.
        Index(
            "uq_ingest_jobs_active_material",
            "material_id",
            unique=True,
            postgresql_where=text("status IN ('QUEUED', 'RUNNING')"),
        ),
        Index("ix_ingest_jobs_queue", "status", "run_after"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    material_id: Mapped[UUID] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE")
    )
    status: Mapped[IngestJobStatus] = mapped_column(default=IngestJobStatus.QUEUED)
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(64))
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class QaLog(TenantMixin, Base):
    """Журнал вопросов и ответов (BH-20).

    Нужен для трёх вещей: привязать 👍/👎 и eval к версии промпта и
    модели, собрать отчёт о пробелах (вопросы, на которые в документах
    ответа нет) и считать расход кредитов компании.

    Вопрос хранится ПОСЛЕ mask_pii (почта, телефоны, паспорта, ФИО):
    для подписи кластеров пробелов текст нужен, но персональные данные в
    нём — нет. Срок хранения — QA_LOG_RETENTION_DAYS, удаляет команда
    purge. Ответ модели не хранится — и для памяти диалога тоже: реплики
    живут в Redis несколько часов (core/dialogue_store.py).
    """

    __tablename__ = "qa_log"
    __table_args__ = (
        Index("ix_qa_log_tenant_created", "tenant_id", "created_at"),
        CheckConstraint("feedback IN (-1, 1)", name="ck_qa_log_feedback"),
        CheckConstraint(
            "origin IN ('documents', 'general_knowledge', 'none')",
            name="ck_qa_log_origin",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    question: Mapped[str] = mapped_column(Text)
    # Тот же вектор, что ушёл в поиск (не считать второй раз); по нему
    # кластеризуются пробелы. Размерность — как у chunks.
    question_embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32))
    # Пусто, если модель не вызывалась (строгий отказ без выдержек).
    llm_model: Mapped[str | None] = mapped_column(String(64))
    # Версия из ответа провайдера (BH-26): алиас /latest молча меняет
    # модель, а метрики по журналу должны быть привязаны к настоящей.
    llm_model_version: Mapped[str | None] = mapped_column(String(128))
    best_vector_distance: Mapped[float | None]
    best_fulltext_score: Mapped[float | None]
    answer_given: Mapped[bool]
    origin: Mapped[str] = mapped_column(String(32))
    source_chunk_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(Uuid), default=list, server_default="{}"
    )
    input_tokens: Mapped[int] = mapped_column(default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(default=0, server_default="0")
    credits: Mapped[int] = mapped_column(default=0, server_default="0")
    feedback: Mapped[int | None] = mapped_column(SmallInteger)
    # Заполняет ночная задача отчёта о пробелах (classify_miss).
    miss_kind: Mapped[str | None] = mapped_column(String(32))
    # Память диалога (BH-28). Сами реплики — в Redis, не здесь
    # (core/dialogue_store.py); журнал знает только, к какому диалогу
    # относится вопрос и как его поняли. standalone_question — после
    # mask_pii, как question; NULL — истории не было, искали по question.
    conversation_id: Mapped[UUID | None] = mapped_column(Uuid)
    standalone_question: Mapped[str | None] = mapped_column(Text)
    condense_prompt_version: Mapped[str | None] = mapped_column(String(32))
    history_turns: Mapped[int] = mapped_column(default=0, server_default="0")
    # Реранкер (M3, BH-32): модель, если порядок выдержек дал он; NULL —
    # порядок вектора (выключен, нечего переставлять или не ответил
    # вовремя). rerank_ms — сколько ждали реранкер, и при сбое тоже.
    rerank_model: Mapped[str | None] = mapped_column(String(128))
    rerank_ms: Mapped[int | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class GlossaryTerm(TenantMixin, Base):
    """Сокращение компании и его расшифровка (M5, BH-14).

    Заполняет админ компании. Перед поиском expand_query (ML) дописывает
    к вопросу расшифровки найденных терминов: «Как оформить ДМС?» находит
    документ, где написано «добровольное медицинское страхование».
    Расшифровки идут только в поиск — в промпт модели уходит исходный
    вопрос сотрудника.
    """

    __tablename__ = "glossary_terms"
    __table_args__ = (
        # «ДМС» и «дмс» — один термин: expand_query ищет без учёта регистра.
        Index(
            "uq_glossary_terms_tenant_term",
            "tenant_id",
            text("lower(term)"),
            unique=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    term: Mapped[str] = mapped_column(String(64))
    expansion: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class GapCluster(TenantMixin, Base):
    """Пробел в документах: группа похожих вопросов без ответа (BH-22).

    Пересобирается ночной задачей (services/gap_report_service.py) из
    qa_log за окно. id и статус переживают пересборку, если группа
    узнаётся по общим вопросам. title и missing — подпись модели
    (промпт gaps-v1) по вопросам после mask_pii.
    """

    __tablename__ = "gap_clusters"
    __table_args__ = (
        Index("ix_gap_clusters_tenant_priority", "tenant_id", "priority"),
        CheckConstraint(
            "status IN ('new', 'in_progress', 'resolved', 'dismissed')",
            name="ck_gap_clusters_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(200))
    missing: Mapped[str] = mapped_column(Text, default="", server_default="")
    priority: Mapped[float] = mapped_column(Float)
    question_count: Mapped[int]
    user_count: Mapped[int]
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="new", server_default="new")
    # Версия промпта подписи; пусто — подписать не удалось, повторить
    # следующей ночью. Смена версии — повод подписать заново.
    prompt_version: Mapped[str] = mapped_column(String(32), default="")
    # Векторы разных моделей эмбеддингов не кластеризуются вместе.
    embedding_model: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class GapClusterQuestion(TenantMixin, Base):
    """Вопрос из qa_log в кластере пробела.

    Удаление строки журнала (срок хранения) убирает её и отсюда.
    """

    __tablename__ = "gap_cluster_questions"

    cluster_id: Mapped[UUID] = mapped_column(
        ForeignKey("gap_clusters.id", ondelete="CASCADE"), primary_key=True
    )
    qa_log_id: Mapped[UUID] = mapped_column(
        ForeignKey("qa_log.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class Connector(TenantMixin, Base):
    """Подключение компании к одной системе-источнику документов.

    kind — система (bitrix24, confluence, yandex360), modules — какие её
    части читать (диск, база знаний, пространства). Один портал — одно
    подключение (решение команды 25.09). config — НЕсекретные настройки
    (адрес портала, корневая папка); credentials — учётные данные режима
    organization, зашифрованные SecretBox, в API никогда не отдаются.
    В режиме per_user учётные данные у каждого сотрудника свои
    (ConnectorUserGrant), здесь пусто.
    """

    __tablename__ = "connectors"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('organization', 'per_user')", name="ck_connectors_mode"
        ),
        CheckConstraint(
            "status IN ('active', 'paused', 'error')", name="ck_connectors_status"
        ),
        CheckConstraint(
            "sync_interval_minutes BETWEEN 15 AND 1440",
            name="ck_connectors_sync_interval",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(100))
    mode: Mapped[str] = mapped_column(String(16))
    modules: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), default=list, server_default="{}"
    )
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}"
    )
    # deferred: шифротекст не должен подниматься в память API на каждый
    # список коннекторов — он нужен только воркеру и ручке проверки.
    credentials: Mapped[str | None] = deferred(mapped_column(Text))
    credentials_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(16), default="active", server_default="active"
    )
    sync_interval_minutes: Mapped[int] = mapped_column(default=60, server_default="60")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ConnectorUserGrant(TenantMixin, Base):
    """Авторизация сотрудника в коннекторе режима per_user.

    Хранит его токены (зашифрованы SecretBox) и состояние: источник
    отверг токен — expired, сотрудник отключился — revoked. Документы
    сотрудника видны ему через material_access, которую заполняет
    синхронизация его листингом.
    """

    __tablename__ = "connector_user_grants"
    __table_args__ = (
        UniqueConstraint("connector_id", "user_id", name="uq_grant_connector_user"),
        CheckConstraint(
            "status IN ('active', 'expired', 'revoked')", name="ck_grants_status"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    connector_id: Mapped[UUID] = mapped_column(
        ForeignKey("connectors.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    credentials: Mapped[str] = deferred(mapped_column(Text))
    # Идентификатор сотрудника в источнике, если адаптер его знает.
    external_user_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(
        String(16), default="active", server_default="active"
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ConnectorSyncRun(TenantMixin, Base):
    """Журнал запусков синхронизации: админу — что и когда произошло,
    команде — разбор сбоев. stats: seen / added / updated / removed /
    skipped / failed. Хранится CONNECTOR_SYNC_RUN_RETENTION_DAYS (purge)."""

    __tablename__ = "connector_sync_runs"
    __table_args__ = (
        Index("ix_sync_runs_connector_started", "connector_id", "started_at"),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'partial', 'failed')",
            name="ck_sync_runs_status",
        ),
        CheckConstraint(
            "trigger IN ('schedule', 'manual')", name="ck_sync_runs_trigger"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    connector_id: Mapped[UUID] = mapped_column(
        ForeignKey("connectors.id", ondelete="CASCADE")
    )
    trigger: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(
        String(16), default="running", server_default="running"
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}"
    )
    error_code: Mapped[str | None] = mapped_column(String(64))


class ConnectorSyncJob(Base):
    """Очередь синхронизации коннекторов — по образцу IngestJob.

    Не под RLS по той же причине: воркер выбирает задачу до того, как
    знает тенанта. Отдельная таблица, а не обобщение ingest_jobs: другой
    payload (коннектор, а не материал) и другая семантика повтора.
    """

    __tablename__ = "connector_sync_jobs"
    __table_args__ = (
        Index(
            "uq_sync_jobs_active_connector",
            "connector_id",
            unique=True,
            postgresql_where=text("status IN ('QUEUED', 'RUNNING')"),
        ),
        Index("ix_sync_jobs_queue", "status", "run_after"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    connector_id: Mapped[UUID] = mapped_column(
        ForeignKey("connectors.id", ondelete="CASCADE")
    )
    trigger: Mapped[str] = mapped_column(
        String(16), default="schedule", server_default="schedule"
    )
    status: Mapped[IngestJobStatus] = mapped_column(default=IngestJobStatus.QUEUED)
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(64))
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
