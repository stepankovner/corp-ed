"""chat retention term per company

Revision ID: e8c2a4f61d93
Revises: b2c3d4e5f6a7
Create Date: 2026-10-09 12:00:00

Решение владельца 09.10: диалоги чата хранятся не бессрочно, а столько
месяцев без активности, сколько выбрал администратор компании (1–36),
по умолчанию 12. Старые удаляет purge. Существующие компании получают
12 через значение по умолчанию столбца.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e8c2a4f61d93"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "chat_retention_months", sa.Integer(), server_default="12", nullable=False
        ),
    )
    op.create_check_constraint(
        "ck_tenants_chat_retention_months",
        "tenants",
        "chat_retention_months BETWEEN 1 AND 36",
    )


def downgrade() -> None:
    op.drop_constraint("ck_tenants_chat_retention_months", "tenants", type_="check")
    op.drop_column("tenants", "chat_retention_months")
