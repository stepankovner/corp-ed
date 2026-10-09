"""members: when personal data was purged after leaving

Revision ID: c7d1e9a4b2f0
Revises: 5ea4e8075af2
Create Date: 2026-10-09 18:00:00

Ушедший из компании (сам, убран администратором или удалил учётку)
остаётся строкой членства ради ссылок журналов. Через 30 дней после
ухода purge удаляет его данные в компании и отмечает это здесь, чтобы
не обходить те же строки каждую ночь.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c7d1e9a4b2f0"
down_revision: Union[str, Sequence[str], None] = "5ea4e8075af2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("data_purged_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "data_purged_at")
