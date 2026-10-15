"""connector_sync_runs.trigger: обход агента сетевых папок ('agent')

Revision ID: d7e1a2b3c4f5
Revises: c43a7f2e9b10
Create Date: 2026-10-15

"""

from collections.abc import Sequence

from alembic import op

revision: str = "d7e1a2b3c4f5"
down_revision: str | Sequence[str] | None = "c43a7f2e9b10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_sync_runs_trigger", "connector_sync_runs", type_="check")
    op.create_check_constraint(
        "ck_sync_runs_trigger",
        "connector_sync_runs",
        "trigger IN ('schedule', 'manual', 'agent')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM connector_sync_runs WHERE trigger = 'agent'")
    op.drop_constraint("ck_sync_runs_trigger", "connector_sync_runs", type_="check")
    op.create_check_constraint(
        "ck_sync_runs_trigger",
        "connector_sync_runs",
        "trigger IN ('schedule', 'manual')",
    )
