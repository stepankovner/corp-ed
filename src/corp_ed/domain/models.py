import enum
from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

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


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    company_code: Mapped[str] = mapped_column(unique=True, index=True)
    name: Mapped[str]
    # Приостановленная компания (неоплата, окончание пилота, инцидент):
    # вход закрыт, выданные токены перестают приниматься.
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())


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

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    title: Mapped[str]
    content: Mapped[str]
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
    embedding: Mapped[list[float]] = mapped_column(Vector(256))
    model: Mapped[str]
    model_version: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
