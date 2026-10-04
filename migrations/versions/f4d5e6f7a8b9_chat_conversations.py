"""chat: conversations, messages, attachments, suggestions

Revision ID: f4d5e6f7a8b9
Revises: f3c4d5e6f7a8
Create Date: 2026-10-04 09:00:00

ТЗ §6 (этап 6): диалоги на сервере (дерево сообщений: правки вопроса и
«Ответить заново» — ветки), вложения к вопросу вне базы компании,
подсказки вопросов от админа; в qa_log — причина и комментарий к оценке
и число выдержек из вложения. Все новые таблицы — под RLS.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from corp_ed.core.db_policies import rls_statements

revision: str = "f4d5e6f7a8b9"
down_revision: Union[str, Sequence[str], None] = "f3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMBEDDING_DIM = 768

TABLES = (
    "conversations",
    "chat_messages",
    "chat_attachments",
    "chat_attachment_chunks",
    "chat_suggestions",
)


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def upgrade() -> None:
    op.add_column("qa_log", sa.Column("feedback_reason", sa.String(length=32), nullable=True))
    op.add_column("qa_log", sa.Column("feedback_comment", sa.Text(), nullable=True))
    op.add_column(
        "qa_log",
        sa.Column("attachment_chunks", sa.Integer(), server_default="0", nullable=False),
    )

    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_message_id", sa.Uuid(), nullable=True),
        sa.Column("share_token", sa.String(length=64), nullable=True),
        sa.Column("shared_message_id", sa.Uuid(), nullable=True),
        sa.Column("shared_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("share_token", name="uq_conversations_share_token"),
    )
    op.create_index(
        op.f("ix_conversations_tenant_id"), "conversations", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_conversations_owner_updated",
        "conversations",
        ["user_id", "updated_at"],
        unique=False,
    )

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default="complete", nullable=False
        ),
        sa.Column("origin", sa.String(length=32), nullable=True),
        sa.Column(
            "sources",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "attachment_ids", sa.ARRAY(sa.Uuid()), server_default="{}", nullable=False
        ),
        sa.Column("qa_log_id", sa.Uuid(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("feedback", sa.SmallInteger(), nullable=True),
        sa.Column("feedback_reason", sa.String(length=32), nullable=True),
        sa.Column("feedback_comment", sa.Text(), nullable=True),
        _created_at(),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="ck_chat_messages_role"),
        sa.CheckConstraint(
            "status IN ('complete', 'generating', 'stopped', 'failed')",
            name="ck_chat_messages_status",
        ),
        sa.CheckConstraint(
            "origin IS NULL OR origin IN ('documents', 'general_knowledge', 'none')",
            name="ck_chat_messages_origin",
        ),
        sa.CheckConstraint("feedback IN (-1, 1)", name="ck_chat_messages_feedback"),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["parent_id"], ["chat_messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["qa_log_id"], ["qa_log.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_chat_messages_tenant_id"), "chat_messages", ["tenant_id"], unique=False
    )
    op.create_index(
        op.f("ix_chat_messages_parent_id"), "chat_messages", ["parent_id"], unique=False
    )
    op.create_index(
        "ix_chat_messages_conversation",
        "chat_messages",
        ["conversation_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "chat_attachments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("source_format", sa.String(length=16), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("tokens", sa.Integer(), nullable=False),
        _created_at(),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("tenant_id", "user_id", "conversation_id"):
        op.create_index(
            op.f(f"ix_chat_attachments_{column}"),
            "chat_attachments",
            [column],
            unique=False,
        )

    op.create_table(
        "chat_attachment_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("attachment_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "heading_path", sa.ARRAY(sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("embed_text", sa.Text(), server_default="", nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["attachment_id"], ["chat_attachments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "attachment_id", "position", name="uq_chat_attachment_chunk_position"
        ),
    )
    op.create_index(
        op.f("ix_chat_attachment_chunks_tenant_id"),
        "chat_attachment_chunks",
        ["tenant_id"],
        unique=False,
    )

    op.create_table(
        "chat_suggestions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("text", sa.String(length=200), nullable=False),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        _created_at(),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_chat_suggestions_tenant_id"),
        "chat_suggestions",
        ["tenant_id"],
        unique=False,
    )

    for statement in rls_statements(TABLES):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("chat_suggestions")
    op.drop_table("chat_attachment_chunks")
    op.drop_table("chat_attachments")
    op.drop_table("chat_messages")
    op.drop_table("conversations")
    op.drop_column("qa_log", "attachment_chunks")
    op.drop_column("qa_log", "feedback_comment")
    op.drop_column("qa_log", "feedback_reason")
