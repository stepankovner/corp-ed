"""qa_log rerank model and time

Revision ID: e41a6c8d2f05
Revises: d30f7b2c9e14
Create Date: 2026-09-30 17:00:00.000000

Реранкер (M3, BH-32): какая модель дала порядок выдержек ответа и сколько
её ждали. rerank_model NULL — порядок вектора (реранкер выключен, нечего
переставлять или не ответил вовремя). Нужны, чтобы сравнить 👍/👎,
пробелы и время ответа с реранкером и без (контракт BH-32: «в qa_log —
модель реранкера и rerank_ms»).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e41a6c8d2f05'
down_revision: Union[str, Sequence[str], None] = 'd30f7b2c9e14'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('qa_log', sa.Column('rerank_model', sa.String(length=128), nullable=True))
    op.add_column('qa_log', sa.Column('rerank_ms', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('qa_log', 'rerank_ms')
    op.drop_column('qa_log', 'rerank_model')
