"""switch chunk embeddings to text-embeddings-v2 768

Revision ID: 97d70ebf2e07
Revises: 082d6d6c3590
Create Date: 2026-09-25 11:43:55.510333

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '97d70ebf2e07'
down_revision: Union[str, Sequence[str], None] = '082d6d6c3590'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """text-search (256) → text-embeddings-v2 (768), BH-17.

    Старые векторы несовместимы с новой моделью и размерностью — их не
    конвертировать, а пересчитать. Одна поставка на все компании:
    1. чанки удаляются целиком (TRUNCATE — RLS на него не действует;
       DELETE под FORCE ROW LEVEL SECURITY от владельца-несуперпользователя
       удалил бы ноль строк, и ALTER ниже упал бы на старых векторах);
    2. колонка становится vector(768);
    3. каждый материал ставится в очередь ингеста — воркер пересчитает
       всё сам, отдельный запуск reindex не нужен.
    До окончания переингеста поиск пуст, и ассистент отвечает общим
    ответом с пометкой. Порог RAG_FAQ_MAX_DISTANCE=0.51 для v2 меняется
    той же поставкой (BH-18).
    """
    op.execute("TRUNCATE chunks")
    op.execute(
        "ALTER TABLE chunks ALTER COLUMN embedding TYPE vector(768) "
        "USING NULL::vector(768)"
    )

    # Выборка материалов всех компаний — от владельца схемы, для которого
    # под FORCE тоже действует RLS. На время транзакции миграции FORCE
    # снимается и возвращается; снаружи этого окна никто не видит.
    op.execute("ALTER TABLE materials NO FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        UPDATE materials SET status = 'PENDING', status_error = NULL,
                             indexed_at = NULL
        """
    )
    op.execute(
        """
        INSERT INTO ingest_jobs (id, tenant_id, material_id, status, attempts,
                                 run_after, created_at, updated_at)
        SELECT gen_random_uuid(), tenant_id, id, 'QUEUED', 0, now(), now(), now()
        FROM materials
        ON CONFLICT (material_id) WHERE status IN ('QUEUED', 'RUNNING') DO NOTHING
        """
    )
    op.execute("ALTER TABLE materials FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    """Обратно на 256: векторы снова пересчитываются с нуля."""
    op.execute("TRUNCATE chunks")
    op.execute(
        "ALTER TABLE chunks ALTER COLUMN embedding TYPE vector(256) "
        "USING NULL::vector(256)"
    )
