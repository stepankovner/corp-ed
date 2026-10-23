"""qa_log: small talk by rule (BH-48)

Revision ID: b48c2d1e7f05
Revises: b50a1c7e9d21
Create Date: 2026-10-23 20:00:00

Реплики правилом (BH-48): «привет», «спасибо», «кто ты?» получают шаблон
до эмбеддинга, поиска и модели. В журнале — вид реплики, а вектора нет:
question_embedding и embedding_model допускают NULL. Отчёт о пробелах
берёт только строки с вектором текущей модели — NULL отпадает сам.

Откат: строки без вектора удаляются — без вектора им в прежней схеме
места нет (это реплики, а не вопросы).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b48c2d1e7f05"
down_revision: Union[str, Sequence[str], None] = "b50a1c7e9d21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("qa_log", sa.Column("small_talk_rule", sa.String(16)))
    op.alter_column("qa_log", "question_embedding", nullable=True)
    op.alter_column("qa_log", "embedding_model", nullable=True)


def downgrade() -> None:
    op.execute("ALTER TABLE qa_log NO FORCE ROW LEVEL SECURITY")
    op.execute("DELETE FROM qa_log WHERE question_embedding IS NULL")
    op.execute("ALTER TABLE qa_log FORCE ROW LEVEL SECURITY")
    op.alter_column("qa_log", "embedding_model", nullable=False)
    op.alter_column("qa_log", "question_embedding", nullable=False)
    op.drop_column("qa_log", "small_talk_rule")
