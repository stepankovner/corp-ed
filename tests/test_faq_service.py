from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.domain.models import (
    Chunk,
    Material,
    Tenant,
)
from corp_ed.llm.fake import FakeAdapter
from corp_ed.llm.fake_embedding import FakeEmbeddingAdapter
from corp_ed.llm.types import Role
from corp_ed.prompts.faq import NOT_FOUND_ANSWER
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.services.faq_service import FaqService


def _chunk(material: Material, position: int, content: str) -> Chunk:
    return Chunk(
        material_id=material.id,
        position=position,
        heading_path=["Раздел 3", "3.1 Продолжительность"],
        embed_text=content,
        content=content,
        embedding=[0.1] * 256,
        model="fake",
        model_version="fake",
    )


def _service(
    chunk_repo: ChunkRepository,
    fake_embeddings: FakeEmbeddingAdapter,
    fake_llm: FakeAdapter,
    *,
    max_distance: float = 0.6,
    context_max_tokens: int = 3000,
    temperature: float = 0.0,
) -> FaqService:
    return FaqService(
        chunk_repo=chunk_repo,
        embedding_gateway=fake_embeddings,
        llm_gateway=fake_llm,
        limit=5,
        max_distance=max_distance,
        context_max_tokens=context_max_tokens,
        temperature=temperature,
    )


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
    assert result.content == NOT_FOUND_ANSWER
    assert fake_llm.calls == []


async def test_no_answer_when_chunks_too_far(
    session: AsyncSession,
    fake_embeddings: FakeEmbeddingAdapter,
    fake_llm: FakeAdapter,
    material: Material,
    chunk_repo: ChunkRepository,
) -> None:
    chunk = _chunk(material, 0, "Совсем про другое.")
    chunk.embedding = [0.9] + [0.1] * 255
    await chunk_repo.bulk_create([chunk])

    service = _service(chunk_repo, fake_embeddings, fake_llm, max_distance=0.0001)
    result = await service.answer("Вопрос")

    assert result.answer_given is False
    assert result.content == NOT_FOUND_ANSWER
    assert fake_llm.calls == []


async def test_found_chunks_reach_the_prompt(
    faq_service: FaqService,
    fake_llm: FakeAdapter,
    material: Material,
    chunk_repo: ChunkRepository,
) -> None:
    await chunk_repo.bulk_create([_chunk(material, 0, "Отпуск составляет 28 дней.")])

    result = await faq_service.answer("Сколько дней отпуска?")

    assert result.answer_given is True
    assert len(fake_llm.calls) == 1

    # Проверяется не весь текст промпта: он меняется по версиям.
    user_message = next(m for m in fake_llm.calls[0] if m.role is Role.USER)
    assert "Отпуск составляет 28 дней." in user_message.content
    assert "[1]" in user_message.content
    # Выдержка подписана источником: название материала и путь разделов.
    assert material.title in user_message.content
    assert "3.1 Продолжительность" in user_message.content


async def test_sources_filled_on_answer(
    faq_service: FaqService,
    material: Material,
    chunk_repo: ChunkRepository,
) -> None:
    await chunk_repo.bulk_create([_chunk(material, 0, "Отпуск составляет 28 дней.")])

    result = await faq_service.answer("Сколько дней отпуска?")

    assert result.sources
    assert result.sources[0].material_id == material.id
    assert result.sources[0].position == 0
    assert result.sources[0].title == material.title
    assert result.sources[0].heading_path == ["Раздел 3", "3.1 Продолжительность"]


async def test_model_refusal_with_found_chunks_is_not_an_answer(
    chunk_repo: ChunkRepository,
    fake_embeddings: FakeEmbeddingAdapter,
    material: Material,
) -> None:
    """Модель может отказать и при найденных выдержках (BH-7)."""
    await chunk_repo.bulk_create([_chunk(material, 0, "Про другое.")])
    llm = FakeAdapter(content=NOT_FOUND_ANSWER)

    result = await _service(chunk_repo, fake_embeddings, llm).answer("Вопрос")

    assert len(llm.calls) == 1
    assert result.answer_given is False
    assert result.sources == []


async def test_document_clause_citation_is_normalized(
    chunk_repo: ChunkRepository,
    fake_embeddings: FakeEmbeddingAdapter,
    material: Material,
) -> None:
    """[4.2] — номер пункта документа, а не выдержки: фронт его не свяжет."""
    await chunk_repo.bulk_create(
        [_chunk(material, 0, "4.2 Заявление подаётся за 14 дней.")]
    )
    llm = FakeAdapter(content="Заявление подаётся за 14 дней [4.2].")

    result = await _service(chunk_repo, fake_embeddings, llm).answer("Когда?")

    assert result.content == "Заявление подаётся за 14 дней [1]."


async def test_temperature_comes_from_settings(
    chunk_repo: ChunkRepository,
    fake_embeddings: FakeEmbeddingAdapter,
    fake_llm: FakeAdapter,
    material: Material,
) -> None:
    await chunk_repo.bulk_create([_chunk(material, 0, "Отпуск 28 дней.")])

    await _service(chunk_repo, fake_embeddings, fake_llm, temperature=0.0).answer(
        "Сколько?"
    )

    assert fake_llm.call_kwargs[0]["temperature"] == 0.0


async def test_context_budget_limits_excerpts(
    chunk_repo: ChunkRepository,
    fake_embeddings: FakeEmbeddingAdapter,
    fake_llm: FakeAdapter,
    material: Material,
) -> None:
    """Чанк, не влезший в бюджет, не попадает ни в промпт, ни в источники."""
    await chunk_repo.bulk_create(
        [
            _chunk(material, 0, "а" * 30),  # 10 токенов
            _chunk(material, 1, "б" * 30),
        ]
    )

    service = _service(chunk_repo, fake_embeddings, fake_llm, context_max_tokens=15)
    result = await service.answer("Вопрос")

    assert len(result.sources) == 1
    user_message = next(m for m in fake_llm.calls[0] if m.role is Role.USER)
    assert "б" * 30 not in user_message.content


async def test_sources_follow_excerpt_order(
    chunk_repo: ChunkRepository,
    fake_embeddings: FakeEmbeddingAdapter,
    fake_llm: FakeAdapter,
    material: Material,
) -> None:
    """Номер [n] в ответе — позиция в источниках, порядок обязан совпадать."""
    near = _chunk(material, 0, "Ближний.")
    far = _chunk(material, 1, "Дальний.")
    far.embedding = [0.2] * 128 + [0.1] * 128
    await chunk_repo.bulk_create([far, near])

    result = await _service(chunk_repo, fake_embeddings, fake_llm).answer("Вопрос")

    user_message = next(m for m in fake_llm.calls[0] if m.role is Role.USER)
    assert [source.content for source in result.sources] == ["Ближний.", "Дальний."]
    assert user_message.content.index("Ближний.") < user_message.content.index(
        "Дальний."
    )
