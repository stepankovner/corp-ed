import enum
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    DateTime,
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from corp_ed.core.config import EMBEDDING_DIM
from corp_ed.core.database import Base
from corp_ed.domain.mixins import TenantMixin


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
    # Ответ, когда в документах ничего нет: general или strict
    # (NotFoundMode). Выбирается с клиентом при подключении (CLI).
    not_found_mode: Mapped[str] = mapped_column(
        String(16), default="general", server_default="general"
    )


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
        # выдаче и двойная цена эмбеддингов.
        Index(
            "uq_materials_tenant_sha256",
            "tenant_id",
            "source_sha256",
            unique=True,
            postgresql_where=text("source_sha256 IS NOT NULL"),
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Chunk(TenantMixin, Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("material_id", "position", name="uq_chunk_material_position"),
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
    purge. Ответ модели не хранится.
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
