"""qa_log dialogue

Revision ID: c28d1a0e4b51
Revises: b29e5c1a7f30
Create Date: 2026-09-30 13:00:00.000000

Память диалога (BH-28): к какому диалогу относится вопрос, как его поняли
после переписывания (после mask_pii) и сколько реплик истории учтено.
Сами реплики — в Redis (core/dialogue_store.py), не в журнале: ответ
модели не хранится (решение Артёма 30.09). Индекс по conversation_id из
контракта ML не нужен — историю из журнала не читаем.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c28d1a0e4b51'
down_revision: Union[str, Sequence[str], None] = 'b29e5c1a7f30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('qa_log', sa.Column('conversation_id', sa.Uuid(), nullable=True))
    op.add_column('qa_log', sa.Column('standalone_question', sa.Text(), nullable=True))
    op.add_column(
        'qa_log',
        sa.Column('condense_prompt_version', sa.String(length=32), nullable=True),
    )
    op.add_column(
        'qa_log',
        sa.Column('history_turns', sa.Integer(), server_default='0', nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('qa_log', 'history_turns')
    op.drop_column('qa_log', 'condense_prompt_version')
    op.drop_column('qa_log', 'standalone_question')
    op.drop_column('qa_log', 'conversation_id')
