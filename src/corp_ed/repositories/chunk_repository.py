from uuid import UUID

from sqlalchemy import ColumnElement, delete, exists, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnClause

from corp_ed.core.tenant_context import require_tenant
from corp_ed.domain.models import Chunk, Material, MaterialAccess
from corp_ed.domain.types import ChunkMatch, MaterialVisibility

# Та же конфигурация, что у генерируемой колонки chunks.fts: иначе
# вопрос и документ разобьются на разные основы слов. Константа, не
# параметр запроса — в SQL не попадает ничего от клиента.
_TS_CONFIG: ColumnClause[str] = literal_column("'russian'::regconfig")


def _visible_to(viewer: UUID, tenant_id: UUID) -> ColumnElement[bool]:
    """Условие видимости документа сотруднику (права источника).

    visibility = tenant — виден всем; restricted — только при строке в
    material_access. Обязательный аргумент viewer, а не флаг: поиск без
    зрителя — это поиск по чужим правам, такого вызова быть не должно.
    """
    return or_(
        Material.visibility == MaterialVisibility.TENANT.value,
        exists().where(
            MaterialAccess.material_id == Material.id,
            MaterialAccess.user_id == viewer,
            MaterialAccess.tenant_id == tenant_id,
        ),
    )


class ChunkRepository:
    """Доступ к данным чанков в БД."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def bulk_create(self, chunks: list[Chunk]) -> None:
        self.session.add_all(chunks)
        await self.session.flush()

    async def delete_by_material(self, material_id: UUID) -> None:
        """Удалить все чанки материала.

        Фильтр по тенанту здесь обязателен: bulk DELETE идёт мимо
        обоих хуков изоляции — _apply_tenant_filter реагирует только
        на SELECT, _check_tenant_on_write работает с объектами сессии.
        """
        stmt = delete(Chunk).where(
            Chunk.material_id == material_id,
            Chunk.tenant_id == require_tenant(),
        )
        await self.session.execute(stmt)

    async def search(
        self, embedding: list[float], limit: int = 5, *, viewer: UUID
    ) -> list[ChunkMatch]:
        tenant_id = require_tenant()

        distance = Chunk.embedding.cosine_distance(embedding)

        stmt = (
            select(
                Chunk.id,
                Chunk.content,
                Chunk.material_id,
                Chunk.position,
                Chunk.heading_path,
                Material.title,
                Material.source_url,
                distance.label("distance"),
            )
            # Название берётся JOIN'ом, а не копией в chunks: переименование
            # материала сразу видно в источниках ответа, без переингеста.
            .join(Material, Material.id == Chunk.material_id)
            # Фильтры обязательны на ОБЕИХ таблицах: hook вешает
            # with_loader_criteria, а он применяется к загрузке
            # ORM-сущностей. Здесь колоночный select, сущность не
            # грузится — автоматики нет. Второй фильтр не избыточен:
            # он держит изоляцию, даже если чанк однажды окажется
            # привязан к материалу чужого тенанта.
            .where(
                Chunk.tenant_id == tenant_id,
                Material.tenant_id == tenant_id,
                _visible_to(viewer, tenant_id),
            )
            .order_by(distance)
            .limit(limit)
        )

        result = await self.session.execute(stmt)

        return [
            ChunkMatch(
                id=row.id,
                content=row.content,
                material_id=row.material_id,
                position=row.position,
                distance=row.distance,
                title=row.title,
                heading_path=list(row.heading_path),
                source_url=row.source_url,
            )
            for row in result
        ]

    async def search_fulltext(
        self, query: str, embedding: list[float], limit: int, *, viewer: UUID
    ) -> list[ChunkMatch]:
        """Полнотекстовая ветка гибридного поиска (M1, BH-12).

        query — строка от to_fulltext_query («слово1 or слово2 …»), её
        разбирает websearch_to_tsquery: кавычки и минус из вопроса ML
        вычистил заранее, а значение уходит bind-параметром. Пустую строку
        сюда не передавать.

        Расстояние до вопроса считается тем же запросом: порог и отладка
        (/faq/search) видят его и у чанков, которых нет в векторной ветке.

        Фильтр по тенанту явный на обеих таблицах — колоночный select,
        хук изоляции его не видит (как в search).
        """
        tenant_id = require_tenant()

        ts_query = func.websearch_to_tsquery(_TS_CONFIG, query)
        rank = func.ts_rank_cd(Chunk.fts, ts_query)
        distance = Chunk.embedding.cosine_distance(embedding)

        stmt = (
            select(
                Chunk.id,
                Chunk.content,
                Chunk.material_id,
                Chunk.position,
                Chunk.heading_path,
                Material.title,
                Material.source_url,
                distance.label("distance"),
                rank.label("rank"),
            )
            .join(Material, Material.id == Chunk.material_id)
            .where(
                Chunk.tenant_id == tenant_id,
                Material.tenant_id == tenant_id,
                Chunk.fts.op("@@")(ts_query),
                _visible_to(viewer, tenant_id),
            )
            # id — второй ключ: при равном ранге порядок детерминирован.
            .order_by(rank.desc(), Chunk.id)
            .limit(limit)
        )

        result = await self.session.execute(stmt)

        return [
            ChunkMatch(
                id=row.id,
                content=row.content,
                material_id=row.material_id,
                position=row.position,
                distance=row.distance,
                title=row.title,
                heading_path=list(row.heading_path),
                fulltext_rank=row.rank,
                source_url=row.source_url,
            )
            for row in result
        ]
