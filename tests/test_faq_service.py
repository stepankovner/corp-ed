from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import (
    Chunk,
    Material,
    Tenant,
)
from corp_ed.llm.fake import FakeAdapter
from corp_ed.llm.fake_embedding import FakeEmbeddingAdapter
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.services.faq_service import FaqService


async def test_question_is_embedded_with_embed_query(
    faq_service: FaqService,
    fake_embeddings: FakeEmbeddingAdapter,
    tenant_ctx: Tenant,
) -> None:
    await faq_service.answer("Где найти документацию?")

    assert fake_embeddings.query_calls == ["Где найти документацию?"]
    assert fake_embeddings.document_calls == []


async def test_no_answer_when_nothing_found(
    faq_service: FaqService,
    fake_llm: FakeAdapter,
    tenant_ctx: Tenant,
) -> None:
    result = await faq_service.answer("Где найти документацию?")

    assert result.answer_given is False
    assert result.sources == []
    assert fake_llm.calls == []


async def test_no_answer_when_chunks_too_far(
    session: AsyncSession,
    fake_embeddings: FakeEmbeddingAdapter,
    fake_llm: FakeAdapter,
    material: Material,
    chunk_repo: ChunkRepository,
) -> None:
    await chunk_repo.bulk_create(
        [
            Chunk(
                material_id=material.id,
                position=0,
                content="Совсем про другое.",
                embedding=[0.9] + [0.1] * 255,
                model="fake",
                model_version="fake",
            )
        ]
    )

    service = FaqService(
        chunk_repo=chunk_repo,
        embedding_gateway=fake_embeddings,
        llm_gateway=fake_llm,
        limit=5,
        max_distance=0.0001,
    )
    result = await service.answer("Вопрос")

    assert result.answer_given is False
    assert fake_llm.calls == []


async def test_found_chunks_reach_the_prompt(
    faq_service: FaqService,
    fake_llm: FakeAdapter,
    material: Material,
    chunk_repo: ChunkRepository,
) -> None:
    await chunk_repo.bulk_create(
        [
            Chunk(
                material_id=material.id,
                position=0,
                content="Отпуск составляет 28 дней.",
                embedding=[0.1] * 256,
                model="fake",
                model_version="fake",
            )
        ]
    )

    result = await faq_service.answer("Сколько дней отпуска?")

    assert result.answer_given is True
    assert len(fake_llm.calls) == 1

    sent = " ".join(m.content for m in fake_llm.calls[0])
    assert "Отпуск составляет 28 дней." in sent
    assert "Выдержка 1" in sent


async def test_sources_filled_on_answer(
    faq_service: FaqService,
    material: Material,
    chunk_repo: ChunkRepository,
) -> None:
    await chunk_repo.bulk_create(
        [
            Chunk(
                material_id=material.id,
                position=0,
                content="Отпуск составляет 28 дней.",
                embedding=[0.1] * 256,
                model="fake",
                model_version="fake",
            )
        ]
    )

    result = await faq_service.answer("Сколько дней отпуска?")

    assert result.sources
    assert result.sources[0].material_id == material.id
    assert result.sources[0].position == 0
