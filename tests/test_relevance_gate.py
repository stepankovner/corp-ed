"""BH-37: «отвечать ли по документам» и «какие выдержки брать» — раздельно.

Расстояния — косинусные до вектора вопроса FakeEmbeddingAdapter
(все координаты равны): фрагмент на нужном расстоянии собирается в коде.
"""

import math

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.config import EMBEDDING_DIM, RagSettings
from corp_ed.domain.models import Chunk, Material, User
from corp_ed.domain.types import AnswerOrigin, Retriever
from corp_ed.llm.fake import FakeAdapter
from corp_ed.llm.fake_embedding import FakeEmbeddingAdapter
from corp_ed.llm.reranker import FakeReranker, Reranker
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.repositories.glossary_repository import GlossaryRepository
from corp_ed.repositories.qa_log_repository import QaLogRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.services.faq_service import FaqService
from tests.conftest import make_credit_service

QUESTION = "Как зовут главного бухгалтера?"


def _at(distance: float) -> list[float]:
    """Вектор на косинусном расстоянии distance от вектора вопроса."""
    along = 1 - distance
    across = math.sqrt(1 - along**2)
    half = EMBEDDING_DIM // 2
    unit = 1 / math.sqrt(EMBEDDING_DIM)
    return [
        along * unit + across * unit * (1 if i < half else -1)
        for i in range(EMBEDDING_DIM)
    ]


async def _chunks(
    session: AsyncSession, material: Material, distances: list[float]
) -> None:
    session.add_all(
        Chunk(
            material_id=material.id,
            position=position,
            heading_path=[],
            embed_text=f"выдержка {distance:.2f}",
            content=f"выдержка {distance:.2f}",
            embedding=_at(distance),
            model="fake",
            model_version="fake",
        )
        for position, distance in enumerate(distances)
    )
    await session.commit()


def _service(
    session: AsyncSession,
    llm: FakeAdapter,
    *,
    gate: float | None,
    retriever: Retriever = Retriever.VECTOR,
    reranker: Reranker | None = None,
) -> FaqService:
    return FaqService(
        chunk_repo=ChunkRepository(session),
        qa_log_repo=QaLogRepository(session),
        tenant_repo=TenantRepository(session),
        glossary_repo=GlossaryRepository(session),
        credits=make_credit_service(session),
        embedding_gateway=FakeEmbeddingAdapter(),
        llm_gateway=llm,
        session=session,
        limit=5,
        max_distance=0.59,
        context_max_tokens=3000,
        temperature=0.0,
        retriever=retriever,
        fulltext_weight=0.5,
        reranker=reranker,
        gate_distance=gate,
        near_margin=0.05,
    )


def test_vectors_are_at_the_asked_distance() -> None:
    query = [0.1] * EMBEDDING_DIM
    for distance in (0.5, 0.62, 0.72):
        vector = _at(distance)
        cosine = sum(a * b for a, b in zip(query, vector, strict=True)) / (
            math.sqrt(sum(a * a for a in query)) * math.sqrt(sum(b * b for b in vector))
        )
        assert 1 - cosine == pytest.approx(distance)


async def test_without_gate_one_threshold_as_before(
    session: AsyncSession, material: Material, employee: User
) -> None:
    await _chunks(session, material, [0.62, 0.66])
    llm = FakeAdapter(content="Общий ответ.")

    result = await _service(session, llm, gate=None).answer(QUESTION, employee)

    assert result.origin is AnswerOrigin.GENERAL_KNOWLEDGE
    assert result.sources == []
    assert "выдержка" not in llm.calls[0][-1].content


async def test_nearest_in_the_zone_brings_only_close_excerpts(
    session: AsyncSession, material: Material, employee: User
) -> None:
    """Ближайший 0,62 при gate 0,70 — выдержки до 0,62 + 0,05."""
    await _chunks(session, material, [0.62, 0.66, 0.68])
    llm = FakeAdapter(content="Ответ [1].")

    result = await _service(session, llm, gate=0.70).answer(QUESTION, employee)

    assert result.origin is AnswerOrigin.DOCUMENTS
    prompt = llm.calls[0][-1].content
    assert "выдержка 0.62" in prompt and "выдержка 0.66" in prompt
    assert "выдержка 0.68" not in prompt


async def test_close_nearest_keeps_the_excerpt_threshold(
    session: AsyncSession, material: Material, employee: User
) -> None:
    """Ближайший 0,50 — выдержки до 0,59, дальние не добавляются."""
    await _chunks(session, material, [0.50, 0.58, 0.63])
    llm = FakeAdapter(content="Ответ [1].")

    await _service(session, llm, gate=0.70).answer(QUESTION, employee)

    prompt = llm.calls[0][-1].content
    assert "выдержка 0.50" in prompt and "выдержка 0.58" in prompt
    assert "выдержка 0.63" not in prompt


async def test_nearest_past_the_gate_is_no_documents_answer(
    session: AsyncSession, material: Material, employee: User
) -> None:
    await _chunks(session, material, [0.72])
    llm = FakeAdapter(content="Общий ответ.")

    result = await _service(session, llm, gate=0.70).answer(QUESTION, employee)

    assert result.origin is AnswerOrigin.GENERAL_KNOWLEDGE
    assert "выдержка" not in llm.calls[0][-1].content


async def test_hybrid_passes_by_the_gate(
    session: AsyncSession, material: Material, employee: User
) -> None:
    await _chunks(session, material, [0.62])
    llm = FakeAdapter(content="Ответ [1].")

    result = await _service(session, llm, gate=0.70, retriever=Retriever.HYBRID).answer(
        QUESTION, employee
    )

    assert result.origin is AnswerOrigin.DOCUMENTS


async def test_reranker_gets_the_zone_pool(
    session: AsyncSession, material: Material, employee: User
) -> None:
    """С порогом выдержек 0,59 пул реранкера в зоне был бы пуст."""
    await _chunks(session, material, [0.62, 0.64, 0.69])
    reranker = FakeReranker()
    llm = FakeAdapter(content="Ответ [1].")

    await _service(session, llm, gate=0.70, reranker=reranker).answer(
        QUESTION, employee
    )

    assert sorted(reranker.calls[0][1]) == ["выдержка 0.62", "выдержка 0.64"]


async def test_reranker_is_not_called_without_documents_answer(
    session: AsyncSession, material: Material, employee: User
) -> None:
    await _chunks(session, material, [0.72, 0.75])
    reranker = FakeReranker()

    await _service(
        session, FakeAdapter(content="Общий ответ."), gate=0.70, reranker=reranker
    ).answer(QUESTION, employee)

    assert reranker.calls == []


def _rag(**values: object) -> RagSettings:
    base: dict[str, object] = {
        "chunk_tokens": 400,
        "overlap_tokens": 50,
        "faq_limit": 5,
        "faq_max_distance": 0.59,
        "context_max_tokens": 3000,
        "faq_temperature": 0,
        "retriever": "vector",
        "fulltext_weight": 0.5,
    }
    return RagSettings(**(base | values))  # type: ignore[arg-type]


def test_gate_is_off_by_default_and_when_empty() -> None:
    assert _rag().faq_gate_distance is None
    assert _rag(faq_gate_distance="").faq_gate_distance is None
    assert _rag().answer_distance == 0.59
    assert _rag(faq_gate_distance="0.70").answer_distance == 0.70


def test_gate_closer_than_the_excerpt_threshold_is_a_typo() -> None:
    with pytest.raises(ValidationError):
        _rag(faq_gate_distance=0.5)
