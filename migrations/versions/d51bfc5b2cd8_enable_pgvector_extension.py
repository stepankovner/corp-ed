"""enable pgvector extension

Revision ID: d51bfc5b2cd8
Revises: e33fe01cbf3a
Create Date: 2026-09-13 19:34:07.091119

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd51bfc5b2cd8'
down_revision: Union[str, Sequence[str], None] = 'e33fe01cbf3a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    """Downgrade schema."""
    # Расширение не удаляем: DROP EXTENSION снесёт колонки типа vector
    # вместе с данными, а пересчёт эмбеддингов стоит часов работы и денег.
    pass
