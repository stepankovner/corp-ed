"""not found mode strict by default

Revision ID: eaaa2fcbfb12
Revises: 42a0ae63f5ff
Create Date: 2026-09-28 14:00:00.000000

Решение команды 28.09 (Q1): новая компания — в строгом режиме (честный
отказ, как в досье 3.1), пока продукт не решил иначе. Меняется только
значение по умолчанию; режим уже заведённых компаний не трогаем — его
выбирали при подключении.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'eaaa2fcbfb12'
down_revision: Union[str, Sequence[str], None] = '42a0ae63f5ff'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column('tenants', 'not_found_mode', server_default='strict')


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column('tenants', 'not_found_mode', server_default='general')
