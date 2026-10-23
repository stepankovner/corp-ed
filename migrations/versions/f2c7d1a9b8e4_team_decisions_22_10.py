"""team decisions 22.10: retention 3/6/12, request INN, companyless accounts

Revision ID: f2c7d1a9b8e4
Revises: e45b8c1d2f30
Create Date: 2026-10-22 12:00:00

Решения команды 22.10 (docs/DECISIONS.md):

- срок хранения диалогов — только 3, 6 или 12 месяцев, по умолчанию 6:
  компании с 1 месяцем получают 3, с 24 и 36 — 12 (ближайший допустимый;
  у сокративших срок диалоги удалит ближайший purge);
- ИНН в заявке «Подключить компанию» (у прежних заявок — NULL);
- отметки учётки для автоудаления учёток без компании: когда впервые
  вступила в компанию (заполняется по нынешним членствам и одобренным
  заявкам) и когда ушло предупреждение;
- новое подключение синхронизируется раз в 30 минут (было 60);
  существующие подключения не меняются.

Откат: сроки 3/6/12 допустимы и в прежних границах 1–36, поэтому
возвращаются только ограничение и значение по умолчанию.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f2c7d1a9b8e4"
down_revision: Union[str, Sequence[str], None] = "e45b8c1d2f30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # tenants — корень, не под RLS.
    op.execute(
        "UPDATE tenants SET chat_retention_months = CASE "
        "WHEN chat_retention_months < 3 THEN 3 "
        "WHEN chat_retention_months > 12 THEN 12 "
        "ELSE chat_retention_months END "
        "WHERE chat_retention_months NOT IN (3, 6, 12)"
    )
    op.drop_constraint("ck_tenants_chat_retention_months", "tenants", type_="check")
    op.create_check_constraint(
        "ck_tenants_chat_retention_months",
        "tenants",
        "chat_retention_months BETWEEN 3 AND 12",
    )
    op.alter_column("tenants", "chat_retention_months", server_default="6")

    op.add_column("company_requests", sa.Column("inn", sa.String(12), nullable=True))
    op.create_check_constraint(
        "ck_company_requests_inn",
        "company_requests",
        "inn IS NULL OR inn ~ '^([0-9]{10}|[0-9]{12})$'",
    )

    op.add_column(
        "accounts",
        sa.Column("joined_company_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "accounts",
        sa.Column("deletion_warned_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Членства под FORCE RLS: от владельца-несуперпользователя SELECT не
    # увидел бы ни одной строки (core/db_policies.py).
    op.execute("ALTER TABLE users NO FORCE ROW LEVEL SECURITY")
    op.execute(
        "UPDATE accounts SET joined_company_at = joined.first_at "
        "FROM (SELECT account_id, min(created_at) AS first_at FROM users "
        "WHERE account_id IS NOT NULL GROUP BY account_id) AS joined "
        "WHERE accounts.id = joined.account_id"
    )
    op.execute("ALTER TABLE users FORCE ROW LEVEL SECURITY")
    # Одобренная заявка — компания была, даже если её потом удалили.
    op.execute(
        "UPDATE accounts SET joined_company_at = requests.first_at "
        "FROM (SELECT account_id, min(coalesce(decided_at, created_at)) AS first_at "
        "FROM company_requests WHERE status = 'approved' GROUP BY account_id) "
        "AS requests "
        "WHERE accounts.id = requests.account_id "
        "AND accounts.joined_company_at IS NULL"
    )

    op.alter_column("connectors", "sync_interval_minutes", server_default="30")


def downgrade() -> None:
    op.alter_column("connectors", "sync_interval_minutes", server_default="60")
    op.drop_column("accounts", "deletion_warned_at")
    op.drop_column("accounts", "joined_company_at")
    op.drop_constraint("ck_company_requests_inn", "company_requests", type_="check")
    op.drop_column("company_requests", "inn")
    op.alter_column("tenants", "chat_retention_months", server_default="12")
    op.drop_constraint("ck_tenants_chat_retention_months", "tenants", type_="check")
    op.create_check_constraint(
        "ck_tenants_chat_retention_months",
        "tenants",
        "chat_retention_months BETWEEN 1 AND 36",
    )
