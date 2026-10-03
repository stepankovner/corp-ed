"""second factor, trusted devices, sessions

Revision ID: f2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-10-03 23:00:00

ТЗ §3 (решение 03.10): второй фактор для всех — код на почту по
умолчанию, приложение-аутентификатор (TOTP) и ключи доступа (WebAuthn),
резервные коды; «запомнить это устройство» на 30 дней; правила компании
(mfa_policy, allow_remember_device); браузер и адрес у refresh-токенов —
для списка сеансов.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f2b3c4d5e6f7"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("totp_secret", sa.Text(), nullable=True))
    op.add_column(
        "accounts",
        sa.Column("totp_enabled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("accounts", sa.Column("totp_last_step", sa.BigInteger(), nullable=True))

    op.add_column(
        "tenants",
        sa.Column("mfa_policy", sa.String(length=16), server_default="any", nullable=False),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "allow_remember_device", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
    )
    op.create_check_constraint(
        "ck_tenants_mfa_policy", "tenants", "mfa_policy IN ('any', 'strong')"
    )

    op.add_column(
        "refresh_tokens", sa.Column("user_agent", sa.String(length=300), nullable=True)
    )
    op.add_column("refresh_tokens", sa.Column("ip", sa.String(length=64), nullable=True))

    op.create_table(
        "backup_codes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_backup_codes_account_id"), "backup_codes", ["account_id"])

    op.create_table(
        "passkeys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("credential_id", sa.LargeBinary(), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "transports",
            postgresql.ARRAY(sa.String(length=32)),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("credential_id"),
    )
    op.create_index(op.f("ix_passkeys_account_id"), "passkeys", ["account_id"])

    op.create_table(
        "trusted_devices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_agent", sa.String(length=300), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        op.f("ix_trusted_devices_account_id"), "trusted_devices", ["account_id"]
    )

    op.create_table(
        "auth_challenges",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("email_code_hash", sa.String(length=64), nullable=True),
        sa.Column("webauthn_challenge", sa.LargeBinary(), nullable=True),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("remember", sa.Boolean(), server_default=sa.false(), nullable=False),
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
            "purpose IN ('login', 'totp_setup', 'passkey_setup')",
            name="ck_auth_challenges_purpose",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        op.f("ix_auth_challenges_account_id"), "auth_challenges", ["account_id"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_auth_challenges_account_id"), table_name="auth_challenges")
    op.drop_table("auth_challenges")
    op.drop_index(op.f("ix_trusted_devices_account_id"), table_name="trusted_devices")
    op.drop_table("trusted_devices")
    op.drop_index(op.f("ix_passkeys_account_id"), table_name="passkeys")
    op.drop_table("passkeys")
    op.drop_index(op.f("ix_backup_codes_account_id"), table_name="backup_codes")
    op.drop_table("backup_codes")
    op.drop_column("refresh_tokens", "ip")
    op.drop_column("refresh_tokens", "user_agent")
    op.drop_constraint("ck_tenants_mfa_policy", "tenants", type_="check")
    op.drop_column("tenants", "allow_remember_device")
    op.drop_column("tenants", "mfa_policy")
    op.drop_column("accounts", "totp_last_step")
    op.drop_column("accounts", "totp_enabled_at")
    op.drop_column("accounts", "totp_secret")
