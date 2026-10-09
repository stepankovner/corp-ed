"""billing: requisites, subscriptions, invoices, acts, payment events

Revision ID: 154ba88dfbdd
Revises: 5ea4e8075af2
Create Date: 2026-10-09 18:00:00

Решения владельца 09.10: оплата подписки и пакетов счётом юрлицу или
картой с чеком, ежемесячный акт, автозачисление по вебхуку банка.
Реквизиты, подписка, счета и акты — под RLS. invoice_refs (сквозной
номер счёта → компания) и payment_events (входящие платежи) — без RLS:
вебхук приходит без компании; в первой только идентификаторы, вторую
разбирает команда. Всё выключено, пока PAYMENTS_PROVIDER=none.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from corp_ed.core.db_policies import rls_statements

revision: str = "154ba88dfbdd"
down_revision: Union[str, Sequence[str], None] = "5ea4e8075af2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ("company_requisites", "subscriptions", "invoices", "acts")


def _now(name: str) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def _payer() -> list[sa.Column]:
    return [
        sa.Column("payer_name", sa.String(length=300), nullable=True),
        sa.Column("payer_inn", sa.String(length=12), nullable=True),
        sa.Column("payer_kpp", sa.String(length=9), nullable=True),
        sa.Column("payer_address", sa.String(length=500), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "company_requisites",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("legal_name", sa.String(length=300), nullable=False),
        sa.Column("inn", sa.String(length=12), nullable=False),
        sa.Column("kpp", sa.String(length=9), nullable=True),
        sa.Column("payer_type", sa.String(length=8), nullable=False),
        sa.Column("address", sa.String(length=500), nullable=False),
        sa.Column("documents_email", sa.String(length=254), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        _now("updated_at"),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "payer_type IN ('company', 'ip')", name="ck_company_requisites_type"
        ),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", name="uq_company_requisites_tenant"),
    )
    op.create_index(
        op.f("ix_company_requisites_tenant_id"),
        "company_requisites",
        ["tenant_id"],
        unique=False,
    )

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tariff", sa.String(length=16), nullable=False),
        sa.Column("seats", sa.Integer(), nullable=False),
        sa.Column("next_seats", sa.Integer(), nullable=True),
        sa.Column("period", sa.String(length=8), nullable=False),
        sa.Column("payment_method", sa.String(length=8), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="awaiting_payment",
            nullable=False,
        ),
        sa.Column("current_start", sa.Date(), nullable=True),
        sa.Column("current_end", sa.Date(), nullable=True),
        sa.Column("card_ref", sa.String(length=64), nullable=True),
        sa.Column("card_amount_kopecks", sa.BigInteger(), nullable=True),
        sa.Column("overdue_notified_for", sa.Date(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('awaiting_payment', 'active', 'overdue', 'cancelled')",
            name="ck_subscriptions_status",
        ),
        sa.CheckConstraint(
            "period IN ('month', 'quarter', 'year')", name="ck_subscriptions_period"
        ),
        sa.CheckConstraint(
            "payment_method IN ('invoice', 'card')",
            name="ck_subscriptions_payment_method",
        ),
        sa.CheckConstraint("seats > 0", name="ck_subscriptions_seats_positive"),
        sa.CheckConstraint(
            "next_seats IS NULL OR next_seats > 0",
            name="ck_subscriptions_next_seats_positive",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", name="uq_subscriptions_tenant"),
    )
    op.create_index(
        op.f("ix_subscriptions_tenant_id"), "subscriptions", ["tenant_id"], unique=False
    )

    op.create_table(
        "invoices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="awaiting_payment",
            nullable=False,
        ),
        sa.Column("payment_method", sa.String(length=8), nullable=False),
        sa.Column("amount_kopecks", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column(
            "lines",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("purpose", sa.String(length=210), nullable=False),
        sa.Column("subscription_id", sa.Uuid(), nullable=True),
        sa.Column("credit_order_id", sa.Uuid(), nullable=True),
        sa.Column("tariff", sa.String(length=16), nullable=True),
        sa.Column("seats", sa.Integer(), nullable=True),
        sa.Column("period", sa.String(length=8), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        *_payer(),
        sa.Column("provider", sa.String(length=16), nullable=True),
        sa.Column("provider_ref", sa.String(length=64), nullable=True),
        sa.Column("payment_url", sa.String(length=2083), nullable=True),
        sa.Column("reminded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        _now("created_at"),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('subscription', 'seats', 'credits')", name="ck_invoices_kind"
        ),
        sa.CheckConstraint(
            "status IN ('awaiting_payment', 'paid', 'cancelled')",
            name="ck_invoices_status",
        ),
        sa.CheckConstraint(
            "payment_method IN ('invoice', 'card')",
            name="ck_invoices_payment_method",
        ),
        sa.CheckConstraint("amount_kopecks > 0", name="ck_invoices_amount_positive"),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["subscriptions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["credit_order_id"], ["credit_orders.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("number", name="uq_invoices_number"),
    )
    op.create_index(
        op.f("ix_invoices_tenant_id"), "invoices", ["tenant_id"], unique=False
    )
    op.create_index(
        op.f("ix_invoices_subscription_id"),
        "invoices",
        ["subscription_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_invoices_credit_order_id"),
        "invoices",
        ["credit_order_id"],
        unique=False,
    )
    op.create_index(
        "ix_invoices_tenant_created", "invoices", ["tenant_id", "created_at"]
    )
    op.create_index(
        "uq_invoices_subscription_period",
        "invoices",
        ["subscription_id", "period_start"],
        unique=True,
        postgresql_where=sa.text("kind = 'subscription' AND status <> 'cancelled'"),
    )

    op.create_table(
        "acts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("amount_kopecks", sa.BigInteger(), nullable=False),
        sa.Column(
            "lines",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "invoice_ids",
            postgresql.ARRAY(sa.Uuid()),
            server_default="{}",
            nullable=False,
        ),
        *_payer(),
        sa.Column("provider", sa.String(length=16), nullable=True),
        sa.Column("provider_ref", sa.String(length=64), nullable=True),
        _now("created_at"),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("amount_kopecks > 0", name="ck_acts_amount_positive"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "month", name="uq_acts_month"),
        sa.UniqueConstraint("tenant_id", "number", name="uq_acts_number"),
    )
    op.create_index(op.f("ix_acts_tenant_id"), "acts", ["tenant_id"], unique=False)

    op.create_table(
        "invoice_refs",
        sa.Column("invoice_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("provider_ref", sa.String(length=64), nullable=True),
        _now("created_at"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("invoice_id"),
        sa.UniqueConstraint("number"),
    )
    op.create_index(
        op.f("ix_invoice_refs_tenant_id"), "invoice_refs", ["tenant_id"], unique=False
    )
    op.create_index(
        op.f("ix_invoice_refs_provider_ref"),
        "invoice_refs",
        ["provider_ref"],
        unique=False,
    )

    op.create_table(
        "payment_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("delivery_key", sa.String(length=64), nullable=False),
        sa.Column("charge_key", sa.String(length=160), nullable=True),
        sa.Column("payment_id", sa.String(length=64), nullable=True),
        sa.Column("amount_kopecks", sa.BigInteger(), nullable=True),
        sa.Column("payer_inn", sa.String(length=12), nullable=True),
        sa.Column("payer_name", sa.String(length=300), nullable=True),
        sa.Column("purpose", sa.String(length=500), nullable=True),
        sa.Column("payment_link_id", sa.String(length=64), nullable=True),
        sa.Column(
            "status", sa.String(length=16), server_default="received", nullable=False
        ),
        sa.Column("problem", sa.String(length=32), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("invoice_id", sa.Uuid(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("resolved_by", sa.Uuid(), nullable=True),
        _now("created_at"),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('received', 'pending', 'matched', 'mismatch', "
            "'unmatched', 'ignored', 'resolved')",
            name="ck_payment_events_status",
        ),
        sa.CheckConstraint(
            "kind IN ('incoming', 'acquiring')", name="ck_payment_events_kind"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["resolved_by"], ["accounts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("delivery_key"),
        sa.UniqueConstraint("charge_key"),
    )
    op.create_index(
        op.f("ix_payment_events_tenant_id"),
        "payment_events",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_payment_events_status_created",
        "payment_events",
        ["status", "created_at"],
        unique=False,
    )

    for statement in rls_statements(TABLES):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("payment_events")
    op.drop_table("invoice_refs")
    op.drop_table("acts")
    op.drop_table("invoices")
    op.drop_table("subscriptions")
    op.drop_table("company_requisites")
