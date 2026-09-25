"""pivot: drop programs, briefs and tracks, rename roles

Revision ID: 577fd323ce34
Revises: 453c802d4852
Create Date: 2026-09-25 11:03:13.352127

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '577fd323ce34'
down_revision: Union[str, Sequence[str], None] = '453c802d4852'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Пивот (досье v3.2, раздел 2): продукт — ассистент для всех сотрудников.

    Уходят программы адаптации, брифы и деление материалов по трекам.
    Роли MANAGER/INTERN переименовываются в ADMIN/EMPLOYEE на месте:
    RENAME VALUE сохраняет существующих пользователей, без пересоздания
    типа и перекладки колонки. В БД хранятся ИМЕНА членов енума
    (SQLAlchemy по умолчанию), поэтому переименовываются заглавные.
    """
    op.drop_index(op.f("ix_programs_tenant_id"), table_name="programs")
    op.drop_table("programs")
    op.drop_index(op.f("ix_briefs_tenant_id"), table_name="briefs")
    op.drop_table("briefs")
    op.drop_column("materials", "track")

    postgresql.ENUM(name="programstatus").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="track").drop(op.get_bind(), checkfirst=True)

    op.execute("ALTER TYPE userrole RENAME VALUE 'MANAGER' TO 'ADMIN'")
    op.execute("ALTER TYPE userrole RENAME VALUE 'INTERN' TO 'EMPLOYEE'")


def downgrade() -> None:
    """Структура восстанавливается, данные программ и брифов — нет.

    Всем материалам проставляется трек MARKETING: исходное значение
    не сохранялось, а колонка обязательная.
    """
    op.execute("ALTER TYPE userrole RENAME VALUE 'ADMIN' TO 'MANAGER'")
    op.execute("ALTER TYPE userrole RENAME VALUE 'EMPLOYEE' TO 'INTERN'")

    track = postgresql.ENUM("MARKETING", "ANALYTICS", name="track")
    programstatus = postgresql.ENUM("DRAFT", "APPROVED", name="programstatus")
    track.create(op.get_bind(), checkfirst=True)
    programstatus.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "materials",
        sa.Column(
            "track",
            postgresql.ENUM("MARKETING", "ANALYTICS", name="track", create_type=False),
            nullable=False,
            server_default="MARKETING",
        ),
    )
    op.alter_column("materials", "track", server_default=None)

    op.create_table(
        "briefs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=False),
        sa.Column(
            "track",
            postgresql.ENUM("MARKETING", "ANALYTICS", name="track", create_type=False),
            nullable=False,
        ),
        sa.Column("role_title", sa.String(), nullable=False),
        sa.Column("goals", sa.String(), nullable=False),
        sa.Column("tasks", sa.String(), nullable=False),
        sa.Column("intern_level", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_briefs_tenant_id"), "briefs", ["tenant_id"])

    op.create_table(
        "programs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("brief_id", sa.Uuid(), nullable=False),
        sa.Column("intern_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(
                "DRAFT", "APPROVED", name="programstatus", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["brief_id"], ["briefs.id"]),
        sa.ForeignKeyConstraint(["intern_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_programs_tenant_id"), "programs", ["tenant_id"])
