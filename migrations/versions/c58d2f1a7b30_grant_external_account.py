"""connector_user_grants: external_account

Revision ID: c58d2f1a7b30
Revises: b47c5e2a9d10
Create Date: 2026-10-29 00:00:00

Под какой учёткой источника подключён сотрудник (почта, логин или имя) —
показывается ему в «Моих подключениях»: подключился не тем аккаунтом —
видно сразу (находка приёмки 28.10). Заполняется при подключении и при
каждой синхронизации; у заведённых раньше — с ближайшей синхронизации.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c58d2f1a7b30"
down_revision: Union[str, Sequence[str], None] = "b47c5e2a9d10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "connector_user_grants", sa.Column("external_account", sa.String(320))
    )


def downgrade() -> None:
    op.drop_column("connector_user_grants", "external_account")
