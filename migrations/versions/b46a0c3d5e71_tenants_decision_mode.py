"""tenants.decision_mode (BH-46)

Revision ID: b46a0c3d5e71
Revises: b49d3e2f8a16
Create Date: 2026-10-23 22:00:00

Схема «решение до ответа» (BH-46): режим компании off / shadow / on;
NULL — умолчание окружения RAG_FAQ_DECISION_MODE (off). Порядок
включения — shadow у всех после договора с Cloud.ru, on у внутреннего
тенанта, on у всех; откат на любом шаге — это поле.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b46a0c3d5e71"
down_revision: Union[str, Sequence[str], None] = "b49d3e2f8a16"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("decision_mode", sa.String(8)))
    op.create_check_constraint(
        "ck_tenants_decision_mode",
        "tenants",
        "decision_mode IN ('off', 'shadow', 'on')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_tenants_decision_mode", "tenants", type_="check")
    op.drop_column("tenants", "decision_mode")
