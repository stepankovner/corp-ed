"""admin: company domains and logo, folders with department access

Revision ID: f5e6f7a8b9c0
Revises: f4d5e6f7a8b9
Create Date: 2026-10-04 12:00:00

ТЗ §5 и §7 (этап 7): домены почты компании, логотип (вне RLS, как фото
профиля), папки загруженных документов с доступом по отделам (под RLS).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from corp_ed.core.db_policies import rls_statements

revision: str = "f5e6f7a8b9c0"
down_revision: Union[str, Sequence[str], None] = "f4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "email_domains",
            sa.ARRAY(sa.String(length=253)),
            server_default="{}",
            nullable=False,
        ),
    )

    op.create_table(
        "tenant_logos",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("version", sa.String(length=16), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id"),
    )

    op.create_table(
        "folders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("restricted", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_folders_tenant_id"), "folders", ["tenant_id"], unique=False)
    op.create_index(
        "uq_folders_tenant_name",
        "folders",
        ["tenant_id", sa.literal_column("lower(name)")],
        unique=True,
    )

    op.create_table(
        "folder_departments",
        sa.Column("folder_id", sa.Uuid(), nullable=False),
        sa.Column("department_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["folder_id"], ["folders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["department_id"], ["departments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("folder_id", "department_id"),
    )
    op.create_index(
        op.f("ix_folder_departments_tenant_id"),
        "folder_departments",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_folder_departments_department_id"),
        "folder_departments",
        ["department_id"],
        unique=False,
    )
    for statement in rls_statements(("folders", "folder_departments")):
        op.execute(statement)

    op.add_column("materials", sa.Column("folder_id", sa.Uuid(), nullable=True))
    op.create_index(
        op.f("ix_materials_folder_id"), "materials", ["folder_id"], unique=False
    )
    op.create_foreign_key(
        "materials_folder_id_fkey",
        "materials",
        "folders",
        ["folder_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("materials_folder_id_fkey", "materials", type_="foreignkey")
    op.drop_index(op.f("ix_materials_folder_id"), table_name="materials")
    op.drop_column("materials", "folder_id")
    op.drop_table("folder_departments")
    op.drop_index("uq_folders_tenant_name", table_name="folders")
    op.drop_index(op.f("ix_folders_tenant_id"), table_name="folders")
    op.drop_table("folders")
    op.drop_table("tenant_logos")
    op.drop_column("tenants", "email_domains")
