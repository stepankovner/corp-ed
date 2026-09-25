import enum
from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import ARRAY, DateTime, ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from corp_ed.core.database import Base
from corp_ed.domain.mixins import TenantMixin


class UserRole(enum.Enum):
    MANAGER = "manager"
    INTERN = "intern"


class Track(enum.Enum):
    MARKETING = "marketing"
    ANALYTICS = "analytics"


class ProgramStatus(enum.Enum):
    DRAFT = "draft"
    APPROVED = "approved"


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    company_code: Mapped[str] = mapped_column(unique=True, index=True)
    name: Mapped[str]


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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Brief(TenantMixin, Base):
    __tablename__ = "briefs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    author_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    track: Mapped[Track]
    role_title: Mapped[str]
    goals: Mapped[str]
    tasks: Mapped[str]
    intern_level: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Program(TenantMixin, Base):
    __tablename__ = "programs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    brief_id: Mapped[UUID] = mapped_column(ForeignKey("briefs.id"))
    intern_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[ProgramStatus] = mapped_column(default=ProgramStatus.DRAFT)
    content: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    brief: Mapped["Brief"] = relationship()


class Material(TenantMixin, Base):
    __tablename__ = "materials"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    track: Mapped[Track]
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
