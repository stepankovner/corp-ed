"""superseded editions (BH-41)

Revision ID: b41e5d0c7a21
Revises: 154ba88dfbdd
Create Date: 2026-10-13 12:31:25

Пометка «заменён» у старой редакции документа (решение Артёма 06.10):
superseded_at — документ не участвует в поиске и ответах, но хранится и
виден в «Источниках»; superseded_by_id — новая редакция, если пометку
поставили по подсказке; edition_hint — подсказка при загрузке
(domain.editions), ждёт ответа администратора. Таблица уже под RLS —
новые колонки политику не меняют.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b41e5d0c7a21"
down_revision: Union[str, Sequence[str], None] = "154ba88dfbdd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FK_NAME = "materials_superseded_by_id_fkey"


def upgrade() -> None:
    op.add_column(
        "materials",
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("materials", sa.Column("superseded_by_id", sa.Uuid(), nullable=True))
    op.add_column(
        "materials",
        sa.Column(
            "edition_hint",
            sa.ARRAY(sa.Uuid()),
            server_default="{}",
            nullable=False,
        ),
    )
    op.create_foreign_key(
        FK_NAME,
        "materials",
        "materials",
        ["superseded_by_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(FK_NAME, "materials", type_="foreignkey")
    op.drop_column("materials", "edition_hint")
    op.drop_column("materials", "superseded_by_id")
    op.drop_column("materials", "superseded_at")
