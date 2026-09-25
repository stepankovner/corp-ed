from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.config import EMBEDDING_DIM
from corp_ed.domain.models import (
    Chunk,
    Material,
    Tenant,
    User,
)
from corp_ed.domain.types import AnswerOrigin
from corp_ed.llm.fake import FakeAdapter
from corp_ed.llm.fake_embedding import FakeEmbeddingAdapter
from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.types import Completion, FinishReason, Message, Role, Usage
from corp_ed.prompts.faq import GENERAL_ANSWER_PREFIX, NOT_FOUND_ANSWER
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.repositories.qa_log_repository import QaLogRepository
from corp_ed.services.faq_service import FaqService


def _chunk(material: Material, position: int, content: str) -> Chunk:
    return Chunk(
        material_id=material.id,
        position=position,
        heading_path=["Раздел 3", "3.1 Продолжительность"],
        embed_text=content,
        content=content,
        embedding=[0.1] * EMBEDDING_DIM,
        model="fake",
        model_version="fake",
    )


class ScriptedLLM(LLMGateway):
    """Отвечает заранее заданными текстами по очереди."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.calls: list[list[Message]] = []

    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
    ) -> Completion:
        self.calls.append(messages)
        return Completion(
            content=self.answers[len(self.calls) - 1],
            finish_reason=FinishReason.COMPLETED,
            usage=Usage(input_tokens=0, output_tokens=0),
            model_version="fake",
            model="fake",
            latency_ms=0,
        )


def _system(messages: list[Message]) -> str:
    return next(m.content for m in messages if m.role is Role.SYSTEM)


def _service(
    chunk_repo: ChunkRepository,
    fake_embeddings: FakeEmbeddingAdapter,
    fake_llm: LLMGateway,
    *,
    max_distance: float = 0.6,
    context_max_tokens: int = 3000,
    temperature: float = 0.0,
) -> FaqService:
    return FaqService(
        chunk_repo=chunk_repo,
        qa_log_repo=QaLogRepository(chunk_repo.session),
        embedding_gateway=fake_embeddings,
        llm_gateway=fake_llm,
        session=chunk_repo.session,
        limit=5,
        max_distance=max_distance,
        context_max_tokens=context_max_tokens,
        temperature=temperature,
    )


async def test_question_is_embedded_with_embed_query(
    faq_service: FaqService,
    fake_embeddings: FakeEmbeddingAdapter,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    await faq_service.answer("Где найти документацию?", employee)

    assert fake_embeddings.query_calls == ["Где найти документацию?"]
    assert fake_embeddings.document_calls == []


async def test_general_answer_when_nothing_found(
    faq_service: FaqService,
    fake_llm: FakeAdapter,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    """В документах ничего нет — ответ из общих знаний со строгой пометкой."""
    result = await faq_service.answer("Где найти документацию?", employee)

    assert result.origin is AnswerOrigin.GENERAL_KNOWLEDGE
    assert result.answer_given is False
    assert result.sources == []
    assert result.content.startswith(GENERAL_ANSWER_PREFIX)
    assert len(fake_llm.calls) == 1
    # Общий промпт: ни одной выдержки, правило «не выдавай за правила компании».
    user_message = next(m for m in fake_llm.calls[0] if m.role is Role.USER)
    assert "Выдержки" not in user_message.content
    assert "из общих знаний" in _system(fake_llm.calls[0])


async def test_general_answer_when_chunks_too_far(
    session: AsyncSession,
    fake_embeddings: FakeEmbeddingAdapter,
    material: Material,
    chunk_repo: ChunkRepository,
    employee: User,
) -> None:
    chunk = _chunk(material, 0, "Совсем про другое.")
    chunk.embedding = [0.9] + [0.1] * (EMBEDDING_DIM - 1)
    await chunk_repo.bulk_create([chunk])
    llm = ScriptedLLM("Обычно так.")

    service = _service(chunk_repo, fake_embeddings, llm, max_distance=0.0001)
    result = await service.answer("Вопрос", employee)

    assert result.origin is AnswerOrigin.GENERAL_KNOWLEDGE
    assert result.sources == []
    # Далёкий чанк в модель не ушёл даже в общем режиме.
    assert all("Совсем про другое." not in m.content for m in llm.calls[0])


async def test_general_answer_is_always_marked(
    chunk_repo: ChunkRepository,
    fake_embeddings: FakeEmbeddingAdapter,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    """Модель потеряла пометку — клиенту ответ без неё уйти не может."""
    llm = ScriptedLLM("По Трудовому кодексу отпуск — 28 дней.")

    result = await _service(chunk_repo, fake_embeddings, llm).answer(
        "Отпуск?", employee
    )

    assert result.content == (
        f"{GENERAL_ANSWER_PREFIX}\nПо Трудовому кодексу отпуск — 28 дней."
    )


async def test_documents_answer_is_marked_as_documents(
    faq_service: FaqService,
    material: Material,
    chunk_repo: ChunkRepository,
    employee: User,
) -> None:
    await chunk_repo.bulk_create([_chunk(material, 0, "Отпуск составляет 28 дней.")])

    result = await faq_service.answer("Сколько дней отпуска?", employee)

    assert result.origin is AnswerOrigin.DOCUMENTS
    assert not result.content.startswith(NOT_FOUND_ANSWER)


async def test_found_chunks_reach_the_prompt(
    faq_service: FaqService,
    fake_llm: FakeAdapter,
    material: Material,
    chunk_repo: ChunkRepository,
    employee: User,
) -> None:
    await chunk_repo.bulk_create([_chunk(material, 0, "Отпуск составляет 28 дней.")])

    result = await faq_service.answer("Сколько дней отпуска?", employee)

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
    employee: User,
) -> None:
    await chunk_repo.bulk_create([_chunk(material, 0, "Отпуск составляет 28 дней.")])

    result = await faq_service.answer("Сколько дней отпуска?", employee)

    assert result.sources
    assert result.sources[0].material_id == material.id
    assert result.sources[0].position == 0
    assert result.sources[0].title == material.title
    assert result.sources[0].heading_path == ["Раздел 3", "3.1 Продолжительность"]


async def test_model_refusal_with_found_chunks_falls_back_to_general(
    chunk_repo: ChunkRepository,
    fake_embeddings: FakeEmbeddingAdapter,
    material: Material,
    employee: User,
) -> None:
    """Выдержки нашлись, но ответа в них нет — тот же общий ответ (BH-7).

    Отказ модели по выдержкам клиенту не показывается: второй вызов идёт
    в общий промпт БЕЗ выдержек, источники пусты.
    """
    await chunk_repo.bulk_create([_chunk(material, 0, "Про другое.")])
    llm = ScriptedLLM(NOT_FOUND_ANSWER, "Как правило, так.")

    result = await _service(chunk_repo, fake_embeddings, llm).answer("Вопрос", employee)

    assert len(llm.calls) == 2
    assert "Про другое." in " ".join(m.content for m in llm.calls[0])
    assert "Про другое." not in " ".join(m.content for m in llm.calls[1])
    assert result.origin is AnswerOrigin.GENERAL_KNOWLEDGE
    assert result.answer_given is False
    assert result.sources == []
    assert result.content == f"{GENERAL_ANSWER_PREFIX}\nКак правило, так."


async def test_document_clause_citation_is_normalized(
    chunk_repo: ChunkRepository,
    fake_embeddings: FakeEmbeddingAdapter,
    material: Material,
    employee: User,
) -> None:
    """[4.2] — номер пункта документа, а не выдержки: фронт его не свяжет."""
    await chunk_repo.bulk_create(
        [_chunk(material, 0, "4.2 Заявление подаётся за 14 дней.")]
    )
    llm = FakeAdapter(content="Заявление подаётся за 14 дней [4.2].")

    result = await _service(chunk_repo, fake_embeddings, llm).answer("Когда?", employee)

    assert result.content == "Заявление подаётся за 14 дней [1]."


async def test_temperature_comes_from_settings(
    chunk_repo: ChunkRepository,
    fake_embeddings: FakeEmbeddingAdapter,
    fake_llm: FakeAdapter,
    material: Material,
    employee: User,
) -> None:
    await chunk_repo.bulk_create([_chunk(material, 0, "Отпуск 28 дней.")])

    await _service(chunk_repo, fake_embeddings, fake_llm, temperature=0.0).answer(
        "Сколько?", employee
    )

    assert fake_llm.call_kwargs[0]["temperature"] == 0.0


async def test_context_budget_limits_excerpts(
    chunk_repo: ChunkRepository,
    fake_embeddings: FakeEmbeddingAdapter,
    fake_llm: FakeAdapter,
    material: Material,
    employee: User,
) -> None:
    """Чанк, не влезший в бюджет, не попадает ни в промпт, ни в источники."""
    await chunk_repo.bulk_create(
        [
            _chunk(material, 0, "а" * 30),  # 10 токенов
            _chunk(material, 1, "б" * 30),
        ]
    )

    service = _service(chunk_repo, fake_embeddings, fake_llm, context_max_tokens=15)
    result = await service.answer("Вопрос", employee)

    assert len(result.sources) == 1
    user_message = next(m for m in fake_llm.calls[0] if m.role is Role.USER)
    assert "б" * 30 not in user_message.content


async def test_sources_follow_excerpt_order(
    chunk_repo: ChunkRepository,
    fake_embeddings: FakeEmbeddingAdapter,
    fake_llm: FakeAdapter,
    material: Material,
    employee: User,
) -> None:
    """Номер [n] в ответе — позиция в источниках, порядок обязан совпадать."""
    near = _chunk(material, 0, "Ближний.")
    far = _chunk(material, 1, "Дальний.")
    far.embedding = [0.2] * (EMBEDDING_DIM // 2) + [0.1] * (EMBEDDING_DIM // 2)
    await chunk_repo.bulk_create([far, near])

    result = await _service(chunk_repo, fake_embeddings, fake_llm).answer(
        "Вопрос", employee
    )

    user_message = next(m for m in fake_llm.calls[0] if m.role is Role.USER)
    assert [source.content for source in result.sources] == ["Ближний.", "Дальний."]
    assert user_message.content.index("Ближний.") < user_message.content.index(
        "Дальний."
    )
