"""materials: employee directory (BH-47)

Revision ID: b47c5e2a9d10
Revises: b46b1d4e6f82
Create Date: 2026-10-24 00:00:00

Справочник «должность → человек» (BH-47): документ оргструктуры, из
которого шаг решения берёт записи кодом. directory_hint — сколько записей
распознано при загрузке (ждёт ответа администратора), is_directory —
администратор согласился. Записи не хранятся — разбираются из текста.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b47c5e2a9d10"
down_revision: Union[str, Sequence[str], None] = "b46b1d4e6f82"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("materials", sa.Column("directory_hint", sa.Integer()))
    op.add_column(
        "materials",
        sa.Column(
            "is_directory", sa.Boolean(), nullable=False, server_default="false"
        ),
    )


def downgrade() -> None:
    op.drop_column("materials", "is_directory")
    op.drop_column("materials", "directory_hint")
