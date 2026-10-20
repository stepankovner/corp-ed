"""origin small_talk (BH-45)

Revision ID: e45b8c1d2f30
Revises: d7e1a2b3c4f5
Create Date: 2026-10-20 12:00:00

«Не вопрос» в общем ответе (BH-45, промпт faq-v2.10): приветствие,
благодарность, вопрос об ассистенте — ответ без пометки «ответа нет»,
origin small_talk в журнале и в сообщениях чата. По этому значению такие
строки не идут в отчёт о пробелах и в счёт вопросов аналитики.

Откат: строки small_talk становятся general_knowledge — ближайшее прежнее
значение (ответ не из документов).
"""

from typing import Sequence, Union

from alembic import op

revision: str = "e45b8c1d2f30"
down_revision: Union[str, Sequence[str], None] = "d7e1a2b3c4f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = "'documents', 'general_knowledge', 'none'"
_NEW = "'documents', 'general_knowledge', 'none', 'small_talk'"


def _constraints(values: str) -> None:
    op.drop_constraint("ck_qa_log_origin", "qa_log", type_="check")
    op.create_check_constraint("ck_qa_log_origin", "qa_log", f"origin IN ({values})")
    op.drop_constraint("ck_chat_messages_origin", "chat_messages", type_="check")
    op.create_check_constraint(
        "ck_chat_messages_origin",
        "chat_messages",
        f"origin IS NULL OR origin IN ({values})",
    )


def upgrade() -> None:
    _constraints(_NEW)


def downgrade() -> None:
    # UPDATE под FORCE RLS от владельца-несуперпользователя не видит ни
    # одной строки (core/db_policies.py), а CHECK проверяет все.
    for table in ("qa_log", "chat_messages"):
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(
            f"UPDATE {table} SET origin = 'general_knowledge' "
            "WHERE origin = 'small_talk'"
        )
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    _constraints(_OLD)
