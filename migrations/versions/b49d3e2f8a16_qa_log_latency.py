"""qa_log: answer latency (BH-49)

Revision ID: b49d3e2f8a16
Revises: b48c2d1e7f05
Create Date: 2026-10-23 21:00:00

Время ответа от вопроса до записи журнала, мс (BH-49, ml-monitoring.md
4.2): без него суточный p95 по компании не посчитать. Прежние строки —
NULL.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b49d3e2f8a16"
down_revision: Union[str, Sequence[str], None] = "b48c2d1e7f05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("qa_log", sa.Column("latency_ms", sa.Integer()))


def downgrade() -> None:
    op.drop_column("qa_log", "latency_ms")
