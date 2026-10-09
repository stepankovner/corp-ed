"""accounts: terms of use accepted separately from data consent

Revision ID: e5b9c2d7a4f1
Revises: d2a8f5c3e1b7
Create Date: 2026-10-09 19:00:00

С 01.09.2025 согласие на обработку персональных данных оформляется
отдельно от других документов (ч. 1 ст. 9 152-ФЗ в ред. 156-ФЗ). При
регистрации теперь две галочки: пользовательское соглашение и согласие;
согласие по-прежнему в consented_at и consent_policy_version, соглашение —
здесь. У прежних учёток (одна общая галочка) — NULL.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5b9c2d7a4f1"
down_revision: Union[str, Sequence[str], None] = "d2a8f5c3e1b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("terms_accepted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "accounts", sa.Column("terms_version", sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("accounts", "terms_version")
    op.drop_column("accounts", "terms_accepted_at")
