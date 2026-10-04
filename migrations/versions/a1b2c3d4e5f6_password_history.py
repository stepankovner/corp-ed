"""password history

Revision ID: a1b2c3d4e5f6
Revises: f7b8c9d0e1f2
Create Date: 2026-10-04 19:00:00

Хеши прежних паролей учётки: после смены или сброса нельзя вернуть
пароль, который заменили (services/passwords.py).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "f7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column(
            "previous_password_hashes",
            sa.ARRAY(sa.Text()),
            server_default="{}",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("accounts", "previous_password_hashes")
