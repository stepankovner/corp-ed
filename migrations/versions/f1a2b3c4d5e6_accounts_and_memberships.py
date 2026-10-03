"""accounts and memberships

Revision ID: f1a2b3c4d5e6
Revises: e41a6c8d2f05
Create Date: 2026-10-03 21:00:00

ТЗ §2–3 (решение 03.10): учётка kronto отдельно от компании.

- accounts — почта, пароль, имя, подтверждение почты; users становится
  членством (роль, статус) со ссылкой на учётку. Существующие users
  переносятся: одна учётка на почту (дубли по компаниям сливаются, берётся
  пароль из последнего входа), почта считается подтверждённой — учётки
  заводили команда и админы.
- refresh_tokens принадлежат учётке; все сессии сбрасываются (войти
  заново).
- invites: код приглашения, одобрение, роль; invite_lookups — поиск
  компании по хешу ссылки или кода (старые ссылки продолжают работать).
- company_requests, email_tokens, outbox_emails — заявки на компанию и
  письма.
- RLS own_membership: свои членства видны во всех компаниях.

Данные под FORCE RLS: владелец схемы без app.tenant_id не видит строк —
переносы идут внутри NO FORCE / FORCE (core/db_policies.py).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from corp_ed.core.db_policies import own_membership_statements

revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "e41a6c8d2f05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("hashed_password", sa.String(), nullable=False),
        sa.Column("first_name", sa.String(length=100), nullable=True),
        sa.Column("last_name", sa.String(length=100), nullable=True),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consented_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consent_policy_version", sa.String(length=64), nullable=True),
        sa.Column("token_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("last_tenant_id", sa.Uuid(), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["last_tenant_id"], ["tenants.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )

    # --- users → членства --------------------------------------------------
    op.add_column("users", sa.Column("account_id", sa.Uuid(), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "status", sa.String(length=16), server_default="active", nullable=False
        ),
    )
    op.add_column(
        "users", sa.Column("left_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.execute("ALTER TABLE users NO FORCE ROW LEVEL SECURITY")
    # Одна учётка на почту: пароль и имя — из членства с последним входом.
    op.execute(
        """
        INSERT INTO accounts (id, email, hashed_password, first_name, last_name,
                              email_verified_at, must_change_password,
                              last_tenant_id, last_login_at, created_at)
        SELECT gen_random_uuid(), picked.email, picked.hashed_password,
               NULLIF(split_part(btrim(picked.full_name), ' ', 1), ''),
               NULLIF(btrim(substr(btrim(picked.full_name),
                      length(split_part(btrim(picked.full_name), ' ', 1)) + 1)), ''),
               picked.first_created, picked.must_change_password,
               picked.tenant_id, picked.last_login_at, picked.first_created
        FROM (
            SELECT DISTINCT ON (lower(email))
                   lower(email) AS email, hashed_password, full_name,
                   must_change_password, tenant_id, last_login_at,
                   min(created_at) OVER (PARTITION BY lower(email)) AS first_created
            FROM users
            ORDER BY lower(email), last_login_at DESC NULLS LAST, created_at DESC
        ) AS picked
        """
    )
    op.execute(
        """
        UPDATE users SET account_id = accounts.id,
                         status = CASE WHEN users.is_active THEN 'active'
                                       ELSE 'blocked' END
        FROM accounts WHERE accounts.email = lower(users.email)
        """
    )
    op.execute("ALTER TABLE users FORCE ROW LEVEL SECURITY")

    op.drop_constraint("uq_user_tenant_email", "users", type_="unique")
    op.drop_column("users", "email")
    op.drop_column("users", "hashed_password")
    op.drop_column("users", "full_name")
    op.drop_column("users", "must_change_password")
    op.drop_column("users", "is_active")
    op.create_foreign_key(
        "users_account_id_fkey",
        "users",
        "accounts",
        ["account_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f("ix_users_account_id"), "users", ["account_id"], unique=False)
    op.create_unique_constraint(
        "uq_user_tenant_account", "users", ["tenant_id", "account_id"]
    )
    op.create_check_constraint(
        "ck_users_status", "users", "status IN ('active', 'blocked', 'pending', 'left')"
    )

    # --- refresh_tokens принадлежат учётке; все сессии — заново -------------
    op.execute("DELETE FROM refresh_tokens")
    op.add_column("refresh_tokens", sa.Column("account_id", sa.Uuid(), nullable=False))
    op.add_column(
        "refresh_tokens",
        sa.Column("remember", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.alter_column("refresh_tokens", "user_id", existing_type=sa.Uuid(), nullable=True)
    op.alter_column(
        "refresh_tokens", "tenant_id", existing_type=sa.Uuid(), nullable=True
    )
    op.create_foreign_key(
        "refresh_tokens_account_id_fkey",
        "refresh_tokens",
        "accounts",
        ["account_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        op.f("ix_refresh_tokens_account_id"), "refresh_tokens", ["account_id"]
    )

    # --- приглашения: код, одобрение, роль; поиск по хешу -------------------
    op.add_column(
        "invites", sa.Column("code_hash", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "invites",
        sa.Column(
            "requires_approval", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    )
    op.add_column(
        "invites",
        sa.Column(
            "role",
            postgresql.ENUM(name="userrole", create_type=False),
            server_default="EMPLOYEE",
            nullable=False,
        ),
    )
    op.create_unique_constraint("invites_code_hash_key", "invites", ["code_hash"])
    op.create_table(
        "invite_lookups",
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("invite_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["invite_id"], ["invites.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("hash"),
    )
    op.create_index(
        op.f("ix_invite_lookups_invite_id"), "invite_lookups", ["invite_id"]
    )
    op.create_index(
        op.f("ix_invite_lookups_tenant_id"), "invite_lookups", ["tenant_id"]
    )
    # Действующие ссылки до 03.10 продолжают работать.
    op.execute("ALTER TABLE invites NO FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        INSERT INTO invite_lookups (hash, tenant_id, invite_id)
        SELECT token_hash, tenant_id, id FROM invites WHERE revoked_at IS NULL
        """
    )
    op.execute("ALTER TABLE invites FORCE ROW LEVEL SECURITY")

    # --- заявки на компанию и письма ----------------------------------------
    op.create_table(
        "company_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("company_name", sa.String(length=200), nullable=False),
        sa.Column("seats", sa.Integer(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="new", nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('new', 'approved', 'rejected', 'cancelled')",
            name="ck_company_requests_status",
        ),
        sa.CheckConstraint(
            "seats IS NULL OR seats > 0", name="ck_company_requests_seats_positive"
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_company_requests_account_id"), "company_requests", ["account_id"]
    )
    op.create_table(
        "email_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=True),
        sa.Column("email", sa.String(length=254), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "purpose IN ('verify_email', 'reset_password', 'change_email', "
            "'revert_email')",
            name="ck_email_tokens_purpose",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_email_tokens_account_purpose", "email_tokens", ["account_id", "purpose"]
    )
    op.create_table(
        "outbox_emails",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("to_email", sa.String(length=254), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("text_body", sa.Text(), nullable=False),
        sa.Column("html_body", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_outbox_emails_pending", "outbox_emails", ["sent_at", "next_attempt_at"]
    )

    for statement in own_membership_statements():
        op.execute(statement)


def downgrade() -> None:
    """Обратно к «компания + почта». Учётка в нескольких компаниях
    раскладывается копиями в каждое членство; членства удалённых учёток
    и ушедших из компании удаляются. Сессии сбрасываются."""
    op.execute("DROP POLICY IF EXISTS own_membership ON users")
    op.drop_index("ix_outbox_emails_pending", table_name="outbox_emails")
    op.drop_table("outbox_emails")
    op.drop_index("ix_email_tokens_account_purpose", table_name="email_tokens")
    op.drop_table("email_tokens")
    op.drop_index(op.f("ix_company_requests_account_id"), table_name="company_requests")
    op.drop_table("company_requests")
    op.drop_index(op.f("ix_invite_lookups_tenant_id"), table_name="invite_lookups")
    op.drop_index(op.f("ix_invite_lookups_invite_id"), table_name="invite_lookups")
    op.drop_table("invite_lookups")
    op.drop_constraint("invites_code_hash_key", "invites", type_="unique")
    op.drop_column("invites", "role")
    op.drop_column("invites", "requires_approval")
    op.drop_column("invites", "code_hash")

    op.execute("DELETE FROM refresh_tokens")
    op.drop_index(op.f("ix_refresh_tokens_account_id"), table_name="refresh_tokens")
    op.drop_constraint(
        "refresh_tokens_account_id_fkey", "refresh_tokens", type_="foreignkey"
    )
    op.drop_column("refresh_tokens", "remember")
    op.drop_column("refresh_tokens", "account_id")
    op.alter_column("refresh_tokens", "tenant_id", existing_type=sa.Uuid(), nullable=False)
    op.alter_column("refresh_tokens", "user_id", existing_type=sa.Uuid(), nullable=False)

    op.add_column("users", sa.Column("email", sa.String(), nullable=True))
    op.add_column("users", sa.Column("hashed_password", sa.String(), nullable=True))
    op.add_column("users", sa.Column("full_name", sa.String(), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "must_change_password", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    )
    op.add_column("users", sa.Column("is_active", sa.Boolean(), nullable=True))
    op.execute("ALTER TABLE users NO FORCE ROW LEVEL SECURITY")
    op.execute("DELETE FROM users WHERE account_id IS NULL OR status = 'left'")
    op.execute(
        """
        UPDATE users SET email = accounts.email,
                         hashed_password = accounts.hashed_password,
                         full_name = NULLIF(btrim(concat_ws(' ', accounts.first_name,
                                                           accounts.last_name)), ''),
                         must_change_password = accounts.must_change_password,
                         is_active = (users.status = 'active')
        FROM accounts WHERE accounts.id = users.account_id
        """
    )
    op.execute("ALTER TABLE users FORCE ROW LEVEL SECURITY")
    op.alter_column("users", "email", existing_type=sa.String(), nullable=False)
    op.alter_column("users", "hashed_password", existing_type=sa.String(), nullable=False)
    op.alter_column("users", "is_active", existing_type=sa.Boolean(), nullable=False)
    op.drop_constraint("ck_users_status", "users", type_="check")
    op.drop_constraint("uq_user_tenant_account", "users", type_="unique")
    op.drop_index(op.f("ix_users_account_id"), table_name="users")
    op.drop_constraint("users_account_id_fkey", "users", type_="foreignkey")
    op.drop_column("users", "left_at")
    op.drop_column("users", "status")
    op.drop_column("users", "account_id")
    op.create_unique_constraint("uq_user_tenant_email", "users", ["tenant_id", "email"])
    op.drop_table("accounts")
