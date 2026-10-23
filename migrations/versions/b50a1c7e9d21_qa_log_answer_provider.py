"""qa_log: answer provider and fallback reason (BH-50)

Revision ID: b50a1c7e9d21
Revises: f2c7d1a9b8e4
Create Date: 2026-10-23 12:00:00

Запасная модель ответа (BH-50): кто ответил — yandex или cloudru (NULL —
модель не звали: строгий отказ без выдержек) и почему резерв — timeout,
error_fast, error_config, error_payment, breaker_open, stream_broken
(NULL — ответила основная). Прежние строки — NULL: резерва тогда не было.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b50a1c7e9d21"
down_revision: Union[str, Sequence[str], None] = "f2c7d1a9b8e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("qa_log", sa.Column("answer_provider", sa.String(16)))
    op.add_column("qa_log", sa.Column("answer_fallback_reason", sa.String(32)))


def downgrade() -> None:
    op.drop_column("qa_log", "answer_fallback_reason")
    op.drop_column("qa_log", "answer_provider")
