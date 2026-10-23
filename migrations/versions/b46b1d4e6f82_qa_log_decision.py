"""qa_log: decision step columns (BH-46)

Revision ID: b46b1d4e6f82
Revises: b46a0c3d5e71
Create Date: 2026-10-23 23:00:00

Журнал схемы «решение до ответа» (BH-46, имена стенда offline_e2e):
режим на момент вопроса, итоговый маршрут, решения по порядку (JSON),
вызовы, токены и время шага решения — отдельно от ответа; остальные поля
стенда — в decision_trace. Прежние строки — NULL.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "b46b1d4e6f82"
down_revision: Union[str, Sequence[str], None] = "b46a0c3d5e71"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    ("decision_mode", sa.String(8)),
    ("decision_route", sa.String(16)),
    ("decision", JSONB()),
    ("decision_calls", sa.Integer()),
    ("decision_input_tokens", sa.Integer()),
    ("decision_output_tokens", sa.Integer()),
    ("decision_latency_ms", sa.Integer()),
    ("decision_trace", JSONB()),
)


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        op.add_column("qa_log", sa.Column(name, type_))


def downgrade() -> None:
    for name, _ in reversed(_COLUMNS):
        op.drop_column("qa_log", name)
