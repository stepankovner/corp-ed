"""department confirmed by an admin

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-10-06 11:00:00

ТЗ §7 (решение владельца 06.10): закрытые папки отдела открыты человеку,
только если отдел подтвердил администратор. Кто уже в отделе — ждёт
подтверждения: кто выбрал отдел сам, а кого назначил администратор, по
базе не различить, а открыть лишнее хуже, чем попросить подтвердить.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "department_confirmed",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "department_confirmed")
