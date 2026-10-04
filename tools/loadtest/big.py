"""«Большая компания» для нагрузочной проверки (docs/LOAD-TEST.md) — ТОЛЬКО
локально.

Добавляет компании loadtest синтетические документы до заданного числа
фрагментов: векторы случайные, текст — заглушка той же длины, что у
настоящего фрагмента (~1 200 знаков). Смысла в них нет: замеряется сам
поиск — расстояние до каждого фрагмента компании и сортировка (индекса по
вектору нет, RISKS: фильтр прав после выборки).

    OWNER_DATABASE_URL=… uv run python tools/loadtest/big.py --chunks 20000
"""

import argparse
import asyncio
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from corp_ed.core.config import EMBEDDING_DIM

PER_MATERIAL = 50  # фрагментов на документ: ~20 страниц
BATCH = 5_000

COUNT = text(
    "SELECT count(*) FROM chunks c JOIN tenants t ON t.id = c.tenant_id"
    " WHERE t.company_code = 'loadtest'"
)

MATERIALS = text(
    """
    INSERT INTO materials (id, tenant_id, title, content, status, visibility,
                           source_format, indexed_at)
    SELECT gen_random_uuid(), t.id, 'Синтетика ' || n, '', 'READY', 'tenant',
           'md', now()
    FROM tenants t, generate_series(1, :count) AS n
    WHERE t.company_code = 'loadtest'
    RETURNING id
    """
)

# Вектор — свой у каждой строки: подзапрос ссылается на номер строки.
CHUNKS = text(
    """
    INSERT INTO chunks (id, tenant_id, material_id, position, heading_path,
                        embed_text, content, embedding, model, model_version)
    SELECT gen_random_uuid(), m.tenant_id, m.id, p,
           ARRAY['Раздел ' || p],
           '', repeat('синтетический текст фрагмента ', 40),
           ARRAY(SELECT random() - 0.5
                 FROM generate_series(1, :dim) WHERE p IS NOT NULL
           )::real[]::vector,
           'synthetic@' || :dim, 'synthetic'
    FROM materials m, generate_series(0, :last) AS p
    WHERE m.id = ANY(:ids)
    """
)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--chunks", type=int, required=True, help="сколько всего")
    args = parser.parse_args()
    engine = create_async_engine(os.environ["OWNER_DATABASE_URL"])
    async with engine.begin() as conn:
        have = int(await conn.scalar(COUNT) or 0)
    missing = max(0, args.chunks - have)
    for start in range(0, missing, BATCH):
        materials = -(-min(BATCH, missing - start) // PER_MATERIAL)
        async with engine.begin() as conn:
            ids = list((await conn.execute(MATERIALS, {"count": materials})).scalars())
            await conn.execute(
                CHUNKS, {"ids": ids, "dim": EMBEDDING_DIM, "last": PER_MATERIAL - 1}
            )
        print(f"+{materials * PER_MATERIAL}", flush=True)
    async with engine.begin() as conn:
        await conn.execute(text("ANALYZE chunks"))
        total = await conn.scalar(COUNT)
    await engine.dispose()
    print(f"фрагментов у компании loadtest: {total}")


if __name__ == "__main__":
    asyncio.run(main())
