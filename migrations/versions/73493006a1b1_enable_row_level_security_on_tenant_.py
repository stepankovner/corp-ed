"""enable row level security on tenant tables

Revision ID: 73493006a1b1
Revises: 1bc0bfec7e55
Create Date: 2026-09-25 11:27:43.494451

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from corp_ed.core.db_policies import rls_statements

# Список таблиц зафиксирован здесь, а не взят из TENANT_TABLES: новые
# тенант-таблицы включают RLS в собственных миграциях, и эта ревизия не
# должна менять смысл задним числом.
TABLES = ("users", "materials", "chunks")


# revision identifiers, used by Alembic.
revision: str = '73493006a1b1'
down_revision: Union[str, Sequence[str], None] = '1bc0bfec7e55'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """RLS — второй рубеж изоляции компаний (RISKS №1).

    Политика: строка видна и пишется, только если её tenant_id совпадает
    с параметром сессии app.tenant_id. Параметр ставит приложение в
    начале каждой транзакции (core/database.py). FORCE — политика
    действует и для владельца таблицы.
    """
    for statement in rls_statements(TABLES):
        op.execute(statement)


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
