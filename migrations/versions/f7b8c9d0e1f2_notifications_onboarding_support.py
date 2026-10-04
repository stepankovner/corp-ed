"""notifications, first steps, support requests

Revision ID: f7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-10-04 16:00:00

ТЗ §8 (этап 9): колокольчик и настройки писем (под RLS), отметки первых
шагов у членства, обращения в поддержку (вне RLS: от учётки).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from corp_ed.core.db_policies import rls_statements

revision: str = "f7b8c9d0e1f2"
down_revision: Union[str, Sequence[str], None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("tips_seen_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "users",
        sa.Column("checklist_hidden_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("link", sa.String(length=300), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_notifications_tenant_id"), "notifications", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_notifications_user_created",
        "notifications",
        ["user_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "notification_settings",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "email_connectors", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column("email_credits", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "email_join_requests",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column(
            "email_weekly_digest",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_index(
        op.f("ix_notification_settings_tenant_id"),
        "notification_settings",
        ["tenant_id"],
        unique=False,
    )
    for statement in rls_statements(("notifications", "notification_settings")):
        op.execute(statement)

    op.create_table(
        "support_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("topic", sa.String(length=16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="new", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "topic IN ('login', 'documents', 'answers', 'billing', 'other')",
            name="ck_support_requests_topic",
        ),
        sa.CheckConstraint(
            "status IN ('new', 'answered', 'closed')",
            name="ck_support_requests_status",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_support_requests_account_id"),
        "support_requests",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        "ix_support_requests_created_at",
        "support_requests",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("support_requests")
    op.drop_table("notification_settings")
    op.drop_table("notifications")
    op.drop_column("users", "checklist_hidden_at")
    op.drop_column("users", "tips_seen_at")
