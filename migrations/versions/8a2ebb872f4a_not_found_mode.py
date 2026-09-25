"""not found mode

Revision ID: 8a2ebb872f4a
Revises: b5fcd7777592
Create Date: 2026-09-25 12:10:17.831438

Р1 (BH-24): режим «ответа в документах нет» — настройка компании,
general (по умолчанию, решение 25.09) или strict (честный отказ).
В strict без выдержек модель не вызывается, поэтому qa_log.llm_model
может быть пуст. CHECK на origin — закрытый список, как в AnswerOrigin.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8a2ebb872f4a'
down_revision: Union[str, Sequence[str], None] = 'b5fcd7777592'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column('qa_log', 'llm_model',
               existing_type=sa.VARCHAR(length=64),
               nullable=True)
    op.add_column('tenants', sa.Column('not_found_mode', sa.String(length=16), server_default='general', nullable=False))
    op.create_check_constraint(
        "ck_tenants_not_found_mode",
        "tenants",
        "not_found_mode IN ('general', 'strict')",
    )
    op.create_check_constraint(
        "ck_qa_log_origin",
        "qa_log",
        "origin IN ('documents', 'general_knowledge', 'none')",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("ck_qa_log_origin", "qa_log", type_="check")
    op.drop_constraint("ck_tenants_not_found_mode", "tenants", type_="check")
    op.drop_column('tenants', 'not_found_mode')
    # UPDATE под FORCE RLS от владельца-несуперпользователя не видит ни
    # одной строки (см. core/db_policies.py), а NOT NULL проверяет все.
    op.execute("ALTER TABLE qa_log NO FORCE ROW LEVEL SECURITY")
    op.execute("UPDATE qa_log SET llm_model = '' WHERE llm_model IS NULL")
    op.execute("ALTER TABLE qa_log FORCE ROW LEVEL SECURITY")
    op.alter_column('qa_log', 'llm_model',
               existing_type=sa.VARCHAR(length=64),
               nullable=False)
