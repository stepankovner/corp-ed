"""Гибридный поиск (M1, BH-12): полнотекстовая ветка, RRF, порог."""

from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.config import EMBEDDING_DIM
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.fulltext import to_fulltext_query
from corp_ed.domain.models import Chunk, Material, QaLog, Tenant, User
from corp_ed.domain.types import AnswerOrigin, Retriever
from corp_ed.llm.fake import FakeAdapter
from corp_ed.llm.fake_embedding import FakeEmbeddingAdapter
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.repositories.glossary_repository import GlossaryRepository
from corp_ed.repositories.qa_log_repository import QaLogRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.services.faq_service import FaqService
from tests.conftest import make_credit_service

# Фейковый эмбеддер отдаёт на вопрос [0.1] * dim.
NEAR = [0.1] * EMBEDDING_DIM  # расстояние 0
VIEWER = uuid4()  # любой сотрудник: материалы фикстур видны всей компании
FAR = [-0.1] * (EMBEDDING_DIM // 2) + [0.1] * (EMBEDDING_DIM // 2)  # 1.0


def make_chunk(
    material: Material, position: int, body: str, vector: list[float]
) -> Chunk:
    return Chunk(
        material_id=material.id,
        position=position,
        embed_text=body,
        content=body,
        embedding=vector,
        model="fake",
        model_version="fake",
    )


def _service(
    session: AsyncSession,
    fake_embeddings: FakeEmbeddingAdapter,
    llm: FakeAdapter,
    retriever: Retriever,
    *,
    max_distance: float = 0.6,
) -> FaqService:
    return FaqService(
        chunk_repo=ChunkRepository(session),
        qa_log_repo=QaLogRepository(session),
        tenant_repo=TenantRepository(session),
        glossary_repo=GlossaryRepository(session),
        credits=make_credit_service(session),
        embedding_gateway=fake_embeddings,
        llm_gateway=llm,
        session=session,
        limit=5,
        max_distance=max_distance,
        context_max_tokens=3000,
        temperature=0.0,
        retriever=retriever,
        fulltext_weight=0.5,
    )


async def _fulltext(repo: ChunkRepository, question: str, limit: int = 10) -> list[str]:
    matches = await repo.search_fulltext(
        to_fulltext_query(question), NEAR, limit=limit, viewer=VIEWER
    )
    return [m.content for m in matches]


# --- полнотекстовая ветка --------------------------------------------------------


async def test_fulltext_matches_russian_word_forms(
    chunk_repo: ChunkRepository, material: Material
) -> None:
    """«отпуска» в вопросе находит «отпуск» в документе: словарь russian."""
    await chunk_repo.bulk_create(
        [
            make_chunk(material, 0, "Ежегодный отпуск — 28 календарных дней.", FAR),
            make_chunk(material, 1, "Командировки оформляет бухгалтерия.", FAR),
        ]
    )

    found = await _fulltext(chunk_repo, "Сколько дней отпуска положено?")

    assert found == ["Ежегодный отпуск — 28 календарных дней."]


async def test_fulltext_ranks_more_matching_words_higher(
    chunk_repo: ChunkRepository, material: Material
) -> None:
    await chunk_repo.bulk_create(
        [
            make_chunk(material, 0, "Отпуск переносится по заявлению.", FAR),
            make_chunk(material, 1, "Перенос отпуска: заявление за две недели.", FAR),
        ]
    )

    matches = await chunk_repo.search_fulltext(
        to_fulltext_query("Как перенести отпуск по заявлению за две недели?"),
        NEAR,
        limit=10,
        viewer=VIEWER,
    )

    assert matches[0].content == "Перенос отпуска: заявление за две недели."
    assert matches[0].fulltext_rank is not None
    assert matches[1].fulltext_rank is not None
    assert matches[0].fulltext_rank > matches[1].fulltext_rank


async def test_fulltext_returns_vector_distance_too(
    chunk_repo: ChunkRepository, material: Material
) -> None:
    await chunk_repo.bulk_create([make_chunk(material, 0, "Отпуск 28 дней.", FAR)])

    [match] = await chunk_repo.search_fulltext("отпуск", NEAR, limit=5, viewer=VIEWER)

    assert match.distance == pytest.approx(1.0)
    assert match.title == material.title


async def test_fts_follows_embed_text(
    session: AsyncSession, chunk_repo: ChunkRepository, material: Material
) -> None:
    """Колонку считает база: правка текста сразу видна поиску."""
    chunk = make_chunk(material, 0, "Командировки.", FAR)
    await chunk_repo.bulk_create([chunk])
    chunk.embed_text = "Отпуск."
    await session.flush()

    assert await _fulltext(chunk_repo, "отпуск") == ["Командировки."]


@pytest.mark.parametrize(
    "question",
    [
        '"отпуск" -перенос',
        "отпуск & (перенос | !больничный)",
        "отпуск'); DROP TABLE chunks; --",
        "отпуск:* <-> перенос",
        "OR or AND and NOT not",
        "\\x00 \x00 отпуск",
    ],
)
async def test_hostile_questions_do_not_break_fulltext(
    chunk_repo: ChunkRepository, material: Material, question: str
) -> None:
    """Операторы tsquery и SQL из вопроса не становятся синтаксисом:
    to_fulltext_query оставляет только слова, значение — bind-параметр,
    а websearch_to_tsquery не падает на любом вводе."""
    await chunk_repo.bulk_create([make_chunk(material, 0, "Отпуск 28 дней.", FAR)])

    query = to_fulltext_query(question)
    matches = (
        await chunk_repo.search_fulltext(query, NEAR, limit=5, viewer=VIEWER)
        if query
        else []
    )

    assert all(m.material_id == material.id for m in matches)
    # Таблица на месте.
    assert await chunk_repo.session.scalar(text("SELECT count(*) FROM chunks")) == 1


async def test_minus_in_question_does_not_exclude(
    chunk_repo: ChunkRepository, material: Material
) -> None:
    """«-перенос» в websearch_to_tsquery значило бы НЕ: чанк про перенос
    исчез бы из выдачи. После to_fulltext_query это обычное слово."""
    await chunk_repo.bulk_create(
        [make_chunk(material, 0, "Перенос отпуска по заявлению.", FAR)]
    )

    assert await _fulltext(chunk_repo, "отпуск -перенос") == [
        "Перенос отпуска по заявлению."
    ]


async def test_fulltext_does_not_see_other_company(
    session: AsyncSession, chunk_repo: ChunkRepository, tenant_ctx: Tenant
) -> None:
    other = Tenant(id=uuid4(), company_code="other", name="Other")
    session.add(other)
    await session.commit()
    with tenant_scope(other.id):
        foreign = Material(tenant_id=other.id, title="Чужой", content="x")
        session.add(foreign)
        await session.flush()
        session.add(make_chunk(foreign, 0, "Отпуск 56 дней.", NEAR))
        await session.commit()

    assert await _fulltext(chunk_repo, "отпуск") == []


# --- слияние и порог -------------------------------------------------------------


async def test_hybrid_brings_fulltext_only_chunk_into_answer(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    material: Material,
    employee: User,
    fake_embeddings: FakeEmbeddingAdapter,
) -> None:
    """Вектор прошёл порог — в промпт идёт вся слитая выдача, в том числе
    чанк, найденный только по словам (контракт M1)."""
    await chunk_repo.bulk_create(
        [
            make_chunk(material, 0, "Общие положения.", NEAR),
            make_chunk(material, 1, "Отпуск — 28 календарных дней.", FAR),
        ]
    )
    llm = FakeAdapter(content="28 дней [2].")

    result = await _service(session, fake_embeddings, llm, Retriever.HYBRID).answer(
        "Сколько дней отпуска?", employee
    )

    assert result.origin is AnswerOrigin.DOCUMENTS
    contents = [s.content for s in result.sources]
    assert "Отпуск — 28 календарных дней." in contents
    fused = next(s for s in result.sources if s.content.startswith("Отпуск"))
    assert fused.fulltext_rank is not None


async def test_vector_mode_keeps_far_chunk_out(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    material: Material,
    employee: User,
    fake_embeddings: FakeEmbeddingAdapter,
) -> None:
    await chunk_repo.bulk_create(
        [
            make_chunk(material, 0, "Общие положения.", NEAR),
            make_chunk(material, 1, "Отпуск — 28 календарных дней.", FAR),
        ]
    )

    result = await _service(
        session, fake_embeddings, FakeAdapter(), Retriever.VECTOR
    ).answer("Сколько дней отпуска?", employee)

    assert [s.content for s in result.sources] == ["Общие положения."]


async def test_hybrid_threshold_is_on_best_vector_distance(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    material: Material,
    employee: User,
    fake_embeddings: FakeEmbeddingAdapter,
) -> None:
    """Полнотекст нашёл точное совпадение, но лучший вектор дальше порога:
    ответа по документам нет. Порог по скору RRF был бы бессмысленным —
    он зависит только от рангов."""
    await chunk_repo.bulk_create(
        [make_chunk(material, 0, "Отпуск — 28 календарных дней.", FAR)]
    )
    llm = FakeAdapter()

    result = await _service(session, fake_embeddings, llm, Retriever.HYBRID).answer(
        "Сколько дней отпуска?", employee
    )

    assert result.origin is AnswerOrigin.GENERAL_KNOWLEDGE
    assert result.sources == []
    # В общий промпт выдержка не ушла.
    assert all("28 календарных" not in m.content for m in llm.calls[0])


async def test_fulltext_signal_is_logged_in_both_modes(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    material: Material,
    employee: User,
    fake_embeddings: FakeEmbeddingAdapter,
) -> None:
    """Для отчёта о пробелах: «точное совпадение было, вектор промахнулся»
    (classify_miss → retrieval_miss) видно только с рангом полнотекста."""
    await chunk_repo.bulk_create(
        [make_chunk(material, 0, "Отпуск — 28 календарных дней.", FAR)]
    )
    for retriever in Retriever:
        await _service(session, fake_embeddings, FakeAdapter(), retriever).answer(
            "Сколько дней отпуска?", employee
        )

    entries = (await session.execute(select(QaLog))).scalars().all()
    assert len(entries) == 2
    assert all(e.best_fulltext_score and e.best_fulltext_score > 0 for e in entries)
    assert all(e.best_vector_distance == pytest.approx(1.0) for e in entries)


async def test_question_without_words_skips_fulltext(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    material: Material,
    employee: User,
    fake_embeddings: FakeEmbeddingAdapter,
) -> None:
    await chunk_repo.bulk_create([make_chunk(material, 0, "Общие положения.", NEAR)])

    result = await _service(
        session, fake_embeddings, FakeAdapter(), Retriever.HYBRID
    ).answer("?!…", employee)

    assert [s.content for s in result.sources] == ["Общие положения."]
    [entry] = (await session.execute(select(QaLog))).scalars().all()
    assert entry.best_fulltext_score is None
