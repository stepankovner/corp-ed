"""credit packs: orders, grants, spends, top-up requests, daily limit

Revision ID: 5ea4e8075af2
Revises: b2c3d4e5f6a7
Create Date: 2026-10-09 12:00:00

Решение владельца 09.10: пул исчерпан — администратор докупает пакет
кредитов (заказ, оплата по счёту, зачисление командой), купленные
кредиты живут 12 месяцев и списываются после пула. Учёт — в своих
таблицах, а не в qa_log: журнал ответов хранится 90 дней. Всё под RLS.
«Попросить администратора пополнить» — одна строка на эпизод
исчерпания. Личный дневной лимит — настройка компании, по умолчанию
выключен (NULL).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from corp_ed.core.db_policies import rls_statements

revision: str = "5ea4e8075af2"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ("credit_orders", "credit_grants", "credit_spends", "credit_topup_requests")


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def upgrade() -> None:
    op.add_column(
        "tenants", sa.Column("daily_credits_per_member", sa.Integer(), nullable=True)
    )
    op.create_check_constraint(
        "ck_tenants_daily_credits_positive",
        "tenants",
        "daily_credits_per_member IS NULL OR daily_credits_per_member > 0",
    )

    op.create_table(
        "credit_orders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("pack", sa.String(length=32), nullable=False),
        sa.Column("credits", sa.Integer(), nullable=False),
        sa.Column("amount_kopecks", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="awaiting_payment",
            nullable=False,
        ),
        sa.Column(
            "payment_method",
            sa.String(length=16),
            server_default="invoice",
            nullable=False,
        ),
        sa.Column("invoice_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        _created_at(),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('awaiting_payment', 'paid', 'cancelled')",
            name="ck_credit_orders_status",
        ),
        sa.CheckConstraint(
            "payment_method IN ('invoice', 'card')",
            name="ck_credit_orders_payment_method",
        ),
        sa.CheckConstraint("credits > 0", name="ck_credit_orders_credits_positive"),
        sa.CheckConstraint(
            "amount_kopecks > 0", name="ck_credit_orders_amount_positive"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "number", name="uq_credit_orders_number"),
    )
    op.create_index(
        op.f("ix_credit_orders_tenant_id"), "credit_orders", ["tenant_id"], unique=False
    )

    op.create_table(
        "credit_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("credits", sa.Integer(), nullable=False),
        sa.Column("remaining", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        _created_at(),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("credits > 0", name="ck_credit_grants_credits_positive"),
        sa.CheckConstraint(
            "source IN ('purchase', 'manual')", name="ck_credit_grants_source"
        ),
        sa.CheckConstraint(
            "(source = 'purchase') = (order_id IS NOT NULL)",
            name="ck_credit_grants_order",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["credit_orders.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id"),
    )
    op.create_index(
        op.f("ix_credit_grants_tenant_id"), "credit_grants", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_credit_grants_tenant_expires",
        "credit_grants",
        ["tenant_id", "expires_at"],
        unique=False,
    )

    op.create_table(
        "credit_spends",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("grant_id", sa.Uuid(), nullable=True),
        sa.Column("credits", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        _created_at(),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("credits > 0", name="ck_credit_spends_credits_positive"),
        sa.ForeignKeyConstraint(["grant_id"], ["credit_grants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_credit_spends_tenant_id"), "credit_spends", ["tenant_id"], unique=False
    )
    op.create_index(
        op.f("ix_credit_spends_grant_id"), "credit_spends", ["grant_id"], unique=False
    )
    op.create_index(
        "ix_credit_spends_tenant_period",
        "credit_spends",
        ["tenant_id", "period_start"],
        unique=False,
    )

    op.create_table(
        "credit_topup_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("episode_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        _created_at(),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "episode_start", name="uq_credit_topup_requests_episode"
        ),
    )
    op.create_index(
        op.f("ix_credit_topup_requests_tenant_id"),
        "credit_topup_requests",
        ["tenant_id"],
        unique=False,
    )

    for statement in rls_statements(TABLES):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("credit_topup_requests")
    op.drop_table("credit_spends")
    op.drop_table("credit_grants")
    op.drop_table("credit_orders")
    op.drop_constraint("ck_tenants_daily_credits_positive", "tenants", type_="check")
    op.drop_column("tenants", "daily_credits_per_member")
