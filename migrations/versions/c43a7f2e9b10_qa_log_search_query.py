"""qa_log.search_query (BH-43)

Revision ID: c43a7f2e9b10
Revises: b41e5d0c7a21
Create Date: 2026-10-13 13:05:00

Второй шаг поиска (промпт faq-v2.9): запрос, которым модель попросила
дополнительный поиск, — отдельным полем журнала ответов, после mask_pii.
NULL — второго шага не было. Таблица уже под RLS — новая колонка
политику не меняет.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c43a7f2e9b10"
down_revision: Union[str, Sequence[str], None] = "b41e5d0c7a21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("qa_log", sa.Column("search_query", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("qa_log", "search_query")
