"""profiles, avatars, departments

Revision ID: f3c4d5e6f7a8
Revises: f2b3c4d5e6f7
Create Date: 2026-10-03 21:00:00

ТЗ §4 и §7 (этап 5): отчество, телефон и Telegram в учётке; фото
профиля (account_avatars, вне компаний, как сама учётка); отделы
компании под RLS; должность и отдел — в членстве, свои в каждой
компании.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from corp_ed.core.db_policies import rls_statements

revision: str = "f3c4d5e6f7a8"
down_revision: Union[str, Sequence[str], None] = "f2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("patronymic", sa.String(length=100), nullable=True))
    op.add_column("accounts", sa.Column("phone", sa.String(length=16), nullable=True))
    op.add_column("accounts", sa.Column("telegram", sa.String(length=32), nullable=True))

    op.create_table(
        "account_avatars",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("version", sa.String(length=16), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("account_id"),
    )

    op.create_table(
        "departments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_departments_tenant_id"), "departments", ["tenant_id"], unique=False
    )
    op.create_index(
        "uq_departments_tenant_name",
        "departments",
        ["tenant_id", sa.literal_column("lower(name)")],
        unique=True,
    )
    for statement in rls_statements(("departments",)):
        op.execute(statement)

    op.add_column("users", sa.Column("position", sa.String(length=100), nullable=True))
    op.add_column("users", sa.Column("department_id", sa.Uuid(), nullable=True))
    op.create_index(
        op.f("ix_users_department_id"), "users", ["department_id"], unique=False
    )
    op.create_foreign_key(
        "users_department_id_fkey",
        "users",
        "departments",
        ["department_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("users_department_id_fkey", "users", type_="foreignkey")
    op.drop_index(op.f("ix_users_department_id"), table_name="users")
    op.drop_column("users", "department_id")
    op.drop_column("users", "position")

    op.drop_index("uq_departments_tenant_name", table_name="departments")
    op.drop_index(op.f("ix_departments_tenant_id"), table_name="departments")
    op.drop_table("departments")

    op.drop_table("account_avatars")

    op.drop_column("accounts", "telegram")
    op.drop_column("accounts", "phone")
    op.drop_column("accounts", "patronymic")
