"""tenants: when company data was deleted after termination

Revision ID: d2a8f5c3e1b7
Revises: c7d1e9a4b2f0
Create Date: 2026-10-09 18:30:00

«Удалить данные компании» в нашей панели (и cli delete-tenant) удаляет
всё компании, кроме заказов и начислений кредитов — их хранят для
бухгалтерии пять лет. Строка компании остаётся обезличенной, отметка —
здесь.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d2a8f5c3e1b7"
down_revision: Union[str, Sequence[str], None] = "c7d1e9a4b2f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("data_deleted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenants", "data_deleted_at")
