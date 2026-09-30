"""not found mode general by default

Revision ID: b29e5c1a7f30
Revises: 6a5fa0240814
Create Date: 2026-09-30 12:00:00.000000

Решение Артёма 29.09 (BH-29, отменяет 28.09): новая компания получает
общий ответ с пометкой «В документах компании ответа нет»; строгий отказ —
настройка компании. Обратная к eaaa2fcbfb12. Меняется только значение по
умолчанию: режим заведённых компаний выбирали при подключении, переводит
его команда (`cli set-not-found-mode`, с аудитом).
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "b29e5c1a7f30"
down_revision: Union[str, Sequence[str], None] = "6a5fa0240814"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column("tenants", "not_found_mode", server_default="general")


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column("tenants", "not_found_mode", server_default="strict")
