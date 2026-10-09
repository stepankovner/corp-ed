"""share link expiry

Revision ID: a9c4e2f81b37
Revises: b2c3d4e5f6a7
Create Date: 2026-10-09 12:00:00

ТЗ §6 (решение владельца 09.10): ссылка «поделиться диалогом» живёт
30 дней, владелец продлевает её ещё на столько же. Ссылки, созданные до
миграции, получают 30 дней от момента миграции: когда ими поделились —
неважно, отозвать без предупреждения хуже.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a9c4e2f81b37"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("share_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    # UPDATE под FORCE RLS от владельца-несуперпользователя не видит ни
    # одной строки (см. core/db_policies.py).
    op.execute("ALTER TABLE conversations NO FORCE ROW LEVEL SECURITY")
    op.execute(
        "UPDATE conversations SET share_expires_at = now() + interval '30 days' "
        "WHERE share_token IS NOT NULL"
    )
    op.execute("ALTER TABLE conversations FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_column("conversations", "share_expires_at")
