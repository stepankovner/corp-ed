"""Память диалога (BH-28): уточняющие вопросы сотрудника.

Приёмка из контракта ML (backend-handoff, BH-28) плюс решение Артёма
30.09: реплики — в Redis с окном 12 часов, не в журнале.
"""

import asyncio
import os
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.config import EMBEDDING_DIM
from corp_ed.core.dialogue_store import (
    DialogueKey,
    DialogueStore,
    DialogueStoreUnavailableError,
    InMemoryDialogueStore,
    RedisDialogueStore,
    Remembered,
)
from corp_ed.domain.models import (
    Chunk,
    Department,
    Folder,
    FolderDepartment,
    Material,
    QaLog,
    Tenant,
    User,
    UserRole,
)
from corp_ed.domain.types import AnswerOrigin, NotFoundMode, Retriever
from corp_ed.llm.errors import LLMError
from corp_ed.llm.fake_embedding import FakeEmbeddingAdapter
from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.types import Completion, FinishReason, Message, Role, Usage
from corp_ed.prompts.dialogue import CONDENSE_PROMPT_VERSION, Turn
from corp_ed.prompts.faq import GENERAL_ANSWER_PREFIX
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.repositories.glossary_repository import GlossaryRepository
from corp_ed.repositories.qa_log_repository import QaLogRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.services.faq_service import FaqService
from corp_ed.services.general_answer import REFUSAL_ANSWER
from tests.conftest import make_credit_service
from tests.factories import make_user

FIRST = "Какой максимальный размер гранта в Старт-ИИ-1?"
FOLLOW_UP = "А для УМНИК?"
STANDALONE = "Какой максимальный размер гранта в УМНИК?"


def _is_condense(messages: list[Message]) -> bool:
    return "Перепиши новый вопрос" in messages[0].content


class DialogueLLM(LLMGateway):
    """Переписывание отвечает condensed (или падает), ответ — answer."""

    def __init__(
        self,
        *,
        answer: str = "До 5 млн рублей [1].",
        condensed: str = STANDALONE,
        condense_error: Exception | None = None,
        condense_delay: float = 0.0,
        condense_filtered: bool = False,
    ) -> None:
        self.answer = answer
        self.condensed = condensed
        self.condense_error = condense_error
        self.condense_delay = condense_delay
        self.condense_filtered = condense_filtered
        self.calls: list[list[Message]] = []
        self.kwargs: list[dict[str, Any]] = []

    @property
    def condense_calls(self) -> list[list[Message]]:
        return [call for call in self.calls if _is_condense(call)]

    @property
    def answer_calls(self) -> list[list[Message]]:
        return [call for call in self.calls if not _is_condense(call)]

    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
        response_format: dict[str, Any] | None = None,
    ) -> Completion:
        self.calls.append(messages)
        self.kwargs.append({"temperature": temperature, "max_tokens": max_tokens})
        if _is_condense(messages):
            if self.condense_delay:
                await asyncio.sleep(self.condense_delay)
            if self.condense_error is not None:
                raise self.condense_error
            return Completion(
                content=self.condensed,
                finish_reason=(
                    FinishReason.FILTERED
                    if self.condense_filtered
                    else FinishReason.COMPLETED
                ),
                usage=Usage(input_tokens=300, output_tokens=20),
                model="fake",
                model_version="fake",
                latency_ms=0,
            )
        return Completion(
            content=self.answer,
            finish_reason=FinishReason.COMPLETED,
            usage=Usage(input_tokens=1800, output_tokens=40),
            model="fake",
            model_version="fake",
            latency_ms=0,
        )


class BrokenStore(DialogueStore):
    async def load_remembered(self, key: DialogueKey) -> list[Remembered]:
        raise DialogueStoreUnavailableError

    async def append(
        self,
        key: DialogueKey,
        turn: Turn,
        *,
        keep: int,
        ttl_seconds: int,
        materials: Iterable[UUID] = (),
    ) -> None:
        raise DialogueStoreUnavailableError


def _service(
    session: AsyncSession,
    llm: LLMGateway,
    embeddings: FakeEmbeddingAdapter,
    store: DialogueStore | None,
    *,
    history_turns: int = 3,
    condense_timeout: float = 5.0,
) -> FaqService:
    return FaqService(
        chunk_repo=ChunkRepository(session),
        qa_log_repo=QaLogRepository(session),
        tenant_repo=TenantRepository(session),
        glossary_repo=GlossaryRepository(session),
        credits=make_credit_service(session),
        embedding_gateway=embeddings,
        llm_gateway=llm,
        session=session,
        limit=5,
        max_distance=0.6,
        context_max_tokens=3000,
        temperature=0.0,
        retriever=Retriever.VECTOR,
        fulltext_weight=0.5,
        dialogue_store=store,
        history_turns=history_turns,
        condense_timeout=condense_timeout,
    )


@pytest.fixture
async def grant_doc(session: AsyncSession, material: Material) -> None:
    session.add(
        Chunk(
            material_id=material.id,
            position=0,
            heading_path=["Гранты"],
            embed_text="Старт-ИИ-1 — до 5 млн рублей; УМНИК — до 500 тыс. рублей.",
            content="Старт-ИИ-1 — до 5 млн рублей; УМНИК — до 500 тыс. рублей.",
            embedding=[0.1] * EMBEDDING_DIM,
            model="fake",
            model_version="fake",
        )
    )
    await session.commit()


async def _log(session: AsyncSession, log_id: UUID | None) -> QaLog:
    entry = await session.scalar(select(QaLog).where(QaLog.id == log_id))
    assert entry is not None
    return entry


async def _dialogue(service: FaqService, user: User) -> UUID:
    first = await service.answer(FIRST, user)
    assert first.conversation_id is not None
    return first.conversation_id


# --- первый вопрос и выключатель ------------------------------------------------


@pytest.mark.usefixtures("grant_doc")
async def test_first_question_starts_a_dialogue_without_condense(
    session: AsyncSession, fake_embeddings: FakeEmbeddingAdapter, employee: User
) -> None:
    llm = DialogueLLM()
    service = _service(session, llm, fake_embeddings, InMemoryDialogueStore())

    result = await service.answer(FIRST, employee)

    assert result.conversation_id is not None
    assert llm.condense_calls == []
    # Без истории промпт ответа прежний: ни блока диалога, ни «то есть».
    prompt = llm.answer_calls[0][-1].content
    assert "Начало диалога" not in prompt
    assert "(то есть:" not in prompt
    entry = await _log(session, result.log_id)
    assert entry.conversation_id == result.conversation_id
    assert entry.history_turns == 0
    assert entry.standalone_question is None
    assert entry.condense_prompt_version is None


@pytest.mark.usefixtures("grant_doc")
async def test_follow_up_is_condensed_and_searched_by_standalone(
    session: AsyncSession, fake_embeddings: FakeEmbeddingAdapter, employee: User
) -> None:
    llm = DialogueLLM()
    service = _service(session, llm, fake_embeddings, InMemoryDialogueStore())
    conversation = await _dialogue(service, employee)

    result = await service.answer(FOLLOW_UP, employee, conversation_id=conversation)

    assert result.conversation_id == conversation
    # Переписывание: та же модель, T = 0, короткий ответ, прошлая реплика.
    assert len(llm.condense_calls) == 1
    condense_kwargs = llm.kwargs[llm.calls.index(llm.condense_calls[0])]
    assert condense_kwargs == {"temperature": 0.0, "max_tokens": 100}
    assert FIRST in llm.condense_calls[0][-1].content
    # Поиск — по самостоятельному вопросу, не по «А для УМНИК?».
    assert fake_embeddings.query_calls[-1] == STANDALONE
    # Модель ответа видит вопрос как есть, историю и переписанный вопрос.
    prompt = llm.answer_calls[-1][-1].content
    assert f"Вопрос сотрудника: {FOLLOW_UP} (то есть: {STANDALONE})" in prompt
    assert "Начало диалога" in prompt
    assert "До 5 млн рублей" in prompt
    assert "[1]" not in prompt.split("Начало диалога")[1].split("Вопрос сотрудника")[0]

    assert result.diagnostics is not None
    assert result.diagnostics.standalone_question == STANDALONE
    assert result.diagnostics.history_turns == 1
    # Токены переписывания — в расходе ответа.
    assert result.diagnostics.input_tokens == 1800 + 300
    assert result.diagnostics.output_tokens == 40 + 20

    entry = await _log(session, result.log_id)
    assert entry.question == FOLLOW_UP
    assert entry.standalone_question == STANDALONE
    assert entry.condense_prompt_version == CONDENSE_PROMPT_VERSION
    assert entry.history_turns == 1
    assert entry.input_tokens == 2100


@pytest.mark.usefixtures("grant_doc")
async def test_history_keeps_only_the_last_turns(
    session: AsyncSession, fake_embeddings: FakeEmbeddingAdapter, employee: User
) -> None:
    llm = DialogueLLM()
    store = InMemoryDialogueStore()
    service = _service(session, llm, fake_embeddings, store, history_turns=2)
    conversation = await _dialogue(service, employee)
    for question in ("Второй вопрос?", "Третий вопрос?"):
        await service.answer(question, employee, conversation_id=conversation)

    result = await service.answer(FOLLOW_UP, employee, conversation_id=conversation)

    assert result.diagnostics is not None
    assert result.diagnostics.history_turns == 2
    dialogue = llm.condense_calls[-1][-1].content
    assert FIRST not in dialogue
    assert "Второй вопрос?" in dialogue and "Третий вопрос?" in dialogue


@pytest.mark.usefixtures("grant_doc")
async def test_disabled_memory_neither_condenses_nor_stores(
    session: AsyncSession, fake_embeddings: FakeEmbeddingAdapter, employee: User
) -> None:
    """RAG_HISTORY_TURNS=0 (по умолчанию до замера ML) — как до BH-28."""
    llm = DialogueLLM()
    store = InMemoryDialogueStore()
    service = _service(session, llm, fake_embeddings, store, history_turns=0)
    conversation = await _dialogue(service, employee)

    result = await service.answer(FOLLOW_UP, employee, conversation_id=conversation)

    assert llm.condense_calls == []
    assert fake_embeddings.query_calls[-1] == FOLLOW_UP
    assert result.conversation_id == conversation
    key = DialogueKey(employee.tenant_id, employee.id, conversation)
    assert await store.load(key) == []


@pytest.mark.usefixtures("grant_doc")
async def test_without_store_memory_is_off(
    session: AsyncSession, fake_embeddings: FakeEmbeddingAdapter, employee: User
) -> None:
    llm = DialogueLLM()
    service = _service(session, llm, fake_embeddings, None, history_turns=3)
    conversation = await _dialogue(service, employee)

    await service.answer(FOLLOW_UP, employee, conversation_id=conversation)

    assert llm.condense_calls == []


# --- сбои переписывания и хранилища не ломают ответ -----------------------------


@pytest.mark.usefixtures("grant_doc")
@pytest.mark.parametrize(
    "llm",
    [
        DialogueLLM(condense_error=LLMError("Yandex 503", retryable=True)),
        DialogueLLM(condense_delay=1.0),
    ],
    ids=["error", "timeout"],
)
async def test_condense_failure_falls_back_to_the_question(
    session: AsyncSession,
    fake_embeddings: FakeEmbeddingAdapter,
    employee: User,
    llm: DialogueLLM,
) -> None:
    service = _service(
        session, llm, fake_embeddings, InMemoryDialogueStore(), condense_timeout=0.05
    )
    conversation = await _dialogue(service, employee)

    result = await service.answer(FOLLOW_UP, employee, conversation_id=conversation)

    assert result.origin is AnswerOrigin.DOCUMENTS
    assert fake_embeddings.query_calls[-1] == FOLLOW_UP
    assert result.diagnostics is not None
    # Сбойный вызов не оплачивается: токенов о нём нет.
    assert result.diagnostics.input_tokens == 1800
    # История всё равно в промпте ответа.
    assert "Начало диалога" in llm.answer_calls[-1][-1].content


@pytest.mark.usefixtures("grant_doc")
async def test_filtered_condense_falls_back_and_is_billed(
    session: AsyncSession, fake_embeddings: FakeEmbeddingAdapter, employee: User
) -> None:
    llm = DialogueLLM(condense_filtered=True)
    service = _service(session, llm, fake_embeddings, InMemoryDialogueStore())
    conversation = await _dialogue(service, employee)

    result = await service.answer(FOLLOW_UP, employee, conversation_id=conversation)

    assert fake_embeddings.query_calls[-1] == FOLLOW_UP
    assert result.diagnostics is not None
    assert result.diagnostics.input_tokens == 1800 + 300


@pytest.mark.usefixtures("grant_doc")
async def test_store_failure_answers_without_history(
    session: AsyncSession, fake_embeddings: FakeEmbeddingAdapter, employee: User
) -> None:
    llm = DialogueLLM()
    service = _service(session, llm, fake_embeddings, BrokenStore())

    first = await service.answer(FIRST, employee)
    result = await service.answer(
        FOLLOW_UP, employee, conversation_id=first.conversation_id
    )

    assert result.origin is AnswerOrigin.DOCUMENTS
    assert llm.condense_calls == []


# --- изоляция и срок жизни ------------------------------------------------------


@pytest.mark.usefixtures("grant_doc")
async def test_history_does_not_cross_users(
    session: AsyncSession,
    fake_embeddings: FakeEmbeddingAdapter,
    employee: User,
    tenant_ctx: Tenant,
) -> None:
    """Чужой conversation_id не открывает чужую историю — ключ включает
    сотрудника (утечка — ловушка из контракта BH-28)."""
    other = make_user(
        id=uuid4(),
        tenant_id=tenant_ctx.id,
        email="other@test.com",
        role=UserRole.EMPLOYEE,
        hashed_password="hashed",
    )
    session.add(other)
    await session.commit()
    llm = DialogueLLM()
    service = _service(session, llm, fake_embeddings, InMemoryDialogueStore())
    conversation = await _dialogue(service, employee)

    result = await service.answer(FOLLOW_UP, other, conversation_id=conversation)

    assert llm.condense_calls == []
    assert result.diagnostics is not None
    assert result.diagnostics.history_turns == 0


@pytest.mark.usefixtures("grant_doc")
async def test_history_drops_turns_from_documents_no_longer_visible(
    session: AsyncSession,
    fake_embeddings: FakeEmbeddingAdapter,
    employee: User,
    material: Material,
    tenant_ctx: Tenant,
) -> None:
    """Сотрудника убрали из отдела: реплика по документу закрытой папки
    отдела остаётся в Redis до конца срока, но в модель больше не уходит."""
    department = Department(tenant_id=tenant_ctx.id, name="Гранты")
    folder = Folder(tenant_id=tenant_ctx.id, name="Закрытая", restricted=True)
    session.add_all([department, folder])
    await session.flush()
    session.add(
        FolderDepartment(
            tenant_id=tenant_ctx.id, folder_id=folder.id, department_id=department.id
        )
    )
    material.folder_id = folder.id
    employee.department_id = department.id
    employee.department_confirmed = True
    await session.commit()
    llm = DialogueLLM()
    store = InMemoryDialogueStore()
    service = _service(session, llm, fake_embeddings, store)
    conversation = await _dialogue(service, employee)
    assert (
        await store.load_remembered(
            DialogueKey(employee.tenant_id, employee.id, conversation)
        )
    )[0].materials == {material.id}

    employee.department_id = None
    employee.department_confirmed = False
    await session.commit()
    result = await service.answer(FOLLOW_UP, employee, conversation_id=conversation)

    assert llm.condense_calls == []
    assert result.diagnostics is not None
    assert result.diagnostics.history_turns == 0


async def test_store_key_separates_companies_and_users() -> None:
    store = InMemoryDialogueStore()
    conversation = uuid4()
    mine = DialogueKey(uuid4(), uuid4(), conversation)
    await store.append(mine, Turn("q", "a"), keep=3, ttl_seconds=60)

    for key in (
        DialogueKey(uuid4(), mine.user_id, conversation),
        DialogueKey(mine.tenant_id, uuid4(), conversation),
    ):
        assert await store.load(key) == []
    assert await store.load(mine) == [Turn("q", "a")]


async def test_dialogue_expires_after_ttl_since_last_question() -> None:
    """Окно считается от последнего вопроса: каждый вопрос его продлевает."""
    now = [0.0]
    store = InMemoryDialogueStore(clock=lambda: now[0])
    key = DialogueKey(uuid4(), uuid4(), uuid4())
    ttl = 12 * 3600

    await store.append(key, Turn("q1", "a1"), keep=3, ttl_seconds=ttl)
    now[0] = 1.5 * 3600  # обед
    assert await store.load(key) == [Turn("q1", "a1")]
    await store.append(key, Turn("q2", "a2"), keep=3, ttl_seconds=ttl)
    now[0] = 1.5 * 3600 + ttl - 1
    assert len(await store.load(key)) == 2
    now[0] = 1.5 * 3600 + ttl
    assert await store.load(key) == []


# --- что попадает в историю -----------------------------------------------------


async def test_refusal_is_a_turn_and_general_answer_uses_standalone(
    session: AsyncSession,
    fake_embeddings: FakeEmbeddingAdapter,
    employee: User,
    tenant_ctx: Tenant,
) -> None:
    """Документов нет: первый ответ — строгий отказ, он тоже реплика;
    затем общий режим — общий ответ по переписанному вопросу."""
    tenant_ctx.not_found_mode = NotFoundMode.STRICT.value
    await session.commit()
    llm = DialogueLLM(answer="Обычно до 500 тыс. рублей.")
    store = InMemoryDialogueStore()
    service = _service(session, llm, fake_embeddings, store)

    first = await service.answer(FIRST, employee)
    assert first.content == REFUSAL_ANSWER
    assert first.conversation_id is not None
    key = DialogueKey(employee.tenant_id, employee.id, first.conversation_id)
    assert await store.load(key) == [Turn(FIRST, REFUSAL_ANSWER)]

    tenant_ctx.not_found_mode = NotFoundMode.GENERAL.value
    await session.commit()
    result = await service.answer(
        FOLLOW_UP, employee, conversation_id=first.conversation_id
    )

    assert result.origin is AnswerOrigin.GENERAL_KNOWLEDGE
    assert result.content.startswith(GENERAL_ANSWER_PREFIX)
    general_prompt = next(
        m.content for m in llm.answer_calls[-1] if m.role is Role.USER
    )
    assert STANDALONE in general_prompt


@pytest.mark.usefixtures("grant_doc")
@pytest.mark.parametrize(
    "refusal",
    [REFUSAL_ANSWER, f"{GENERAL_ANSWER_PREFIX}\n\nОбычно гранты до 1 млн рублей."],
    ids=["strict", "general"],
)
async def test_past_no_answer_is_not_shown_to_the_answer_model(
    session: AsyncSession,
    fake_embeddings: FakeEmbeddingAdapter,
    employee: User,
    refusal: str,
) -> None:
    """Владелец 06.10: в диалоге ассистент ответил «в документах ответа
    нет», потом документ стал доступен (подтвердили отдел, загрузили
    файл) — тот же вопрос в том же диалоге снова получал отказ, а в новом
    диалоге — ответ. Прошлый отказ модели ответа не показывается: фактов
    в нём нет, а повторять его модель склонна. Переписыванию вопроса
    история нужна целиком."""
    llm = DialogueLLM(condensed=FIRST)
    store = InMemoryDialogueStore()
    service = _service(session, llm, fake_embeddings, store)
    conversation = uuid4()
    await store.append(
        DialogueKey(employee.tenant_id, employee.id, conversation),
        Turn(FIRST, refusal),
        keep=3,
        ttl_seconds=600,
    )

    result = await service.answer(FIRST, employee, conversation_id=conversation)

    assert result.origin is AnswerOrigin.DOCUMENTS
    assert FIRST in llm.condense_calls[0][-1].content
    prompt = llm.answer_calls[-1][-1].content
    assert "Начало диалога" not in prompt
    assert "Ассистент: В документах" not in prompt
    assert f"Вопрос сотрудника: {FIRST}" in prompt


@pytest.mark.usefixtures("grant_doc")
async def test_no_answer_turns_are_dropped_but_answered_turns_stay(
    session: AsyncSession, fake_embeddings: FakeEmbeddingAdapter, employee: User
) -> None:
    """Из истории для модели ответа уходят только отказы: реплика с
    ответом по документам остаётся — по ней понятен уточняющий вопрос, а
    без последнего отказа модель видит переписанный вопрос."""
    llm = DialogueLLM()
    store = InMemoryDialogueStore()
    service = _service(session, llm, fake_embeddings, store)
    conversation = uuid4()
    key = DialogueKey(employee.tenant_id, employee.id, conversation)
    await store.append(key, Turn(FIRST, "До 5 млн рублей."), keep=3, ttl_seconds=600)
    await store.append(
        key, Turn("Есть ли грант на офис?", REFUSAL_ANSWER), keep=3, ttl_seconds=600
    )

    await service.answer(FOLLOW_UP, employee, conversation_id=conversation)

    prompt = llm.answer_calls[-1][-1].content
    assert "Начало диалога" in prompt
    assert "До 5 млн рублей" in prompt
    assert "Есть ли грант на офис?" not in prompt
    assert "Ассистент: В документах" not in prompt
    assert f"Вопрос сотрудника: {FOLLOW_UP} (то есть: {STANDALONE})" in prompt


@pytest.mark.usefixtures("grant_doc")
async def test_follow_up_after_only_refusals_is_asked_as_standalone(
    session: AsyncSession, fake_embeddings: FakeEmbeddingAdapter, employee: User
) -> None:
    """Вся история — отказы: модель ответа получает переписанный вопрос,
    а не «А для УМНИК?» без контекста."""
    llm = DialogueLLM()
    store = InMemoryDialogueStore()
    service = _service(session, llm, fake_embeddings, store)
    conversation = uuid4()
    await store.append(
        DialogueKey(employee.tenant_id, employee.id, conversation),
        Turn(FIRST, REFUSAL_ANSWER),
        keep=3,
        ttl_seconds=600,
    )

    await service.answer(FOLLOW_UP, employee, conversation_id=conversation)

    prompt = llm.answer_calls[-1][-1].content
    assert "Начало диалога" not in prompt
    assert f"Вопрос сотрудника: {STANDALONE}" in prompt


@pytest.mark.usefixtures("grant_doc")
async def test_stored_question_is_masked(
    session: AsyncSession, fake_embeddings: FakeEmbeddingAdapter, employee: User
) -> None:
    """В хранилище — вопрос как в журнале, после mask_pii."""
    store = InMemoryDialogueStore()
    service = _service(session, DialogueLLM(), fake_embeddings, store)

    result = await service.answer("Напишите ivanov@acme.ru про грант", employee)

    assert result.conversation_id is not None
    turns = await store.load(
        DialogueKey(employee.tenant_id, employee.id, result.conversation_id)
    )
    assert "ivanov@acme.ru" not in turns[0].question
    assert turns[0].answer == result.content


async def test_gap_report_reads_the_standalone_question(
    session: AsyncSession, tenant_ctx: Tenant
) -> None:
    """В подпись пробела — «размер гранта в УМНИК», а не «А для УМНИК?»."""
    for question, standalone in ((FOLLOW_UP, STANDALONE), ("Отпуск?", None)):
        session.add(
            QaLog(
                question=question,
                standalone_question=standalone,
                question_embedding=[0.1] * EMBEDDING_DIM,
                embedding_model="m",
                prompt_version="p",
                answer_given=False,
                origin="none",
                miss_kind="gap",
            )
        )
    await session.commit()

    rows = await QaLogRepository(session).gap_candidates(
        since=datetime(2000, 1, 1, tzinfo=UTC),
        kinds=["gap"],
        embedding_model="m",
        limit=10,
    )

    assert sorted(row.question for row in rows) == sorted([STANDALONE, "Отпуск?"])


# --- Redis ----------------------------------------------------------------------

REDIS_URL = os.environ.get("TEST_REDIS_URL")


@pytest.mark.skipif(REDIS_URL is None, reason="TEST_REDIS_URL не задан")
async def test_redis_store_keeps_last_turns_with_sliding_ttl() -> None:
    assert REDIS_URL is not None
    redis = Redis.from_url(REDIS_URL)
    store = RedisDialogueStore(redis)
    key = DialogueKey(uuid4(), uuid4(), uuid4())
    try:
        for n in range(4):
            await store.append(key, Turn(f"q{n}", f"a{n}"), keep=3, ttl_seconds=600)

        assert await store.load(key) == [Turn(f"q{n}", f"a{n}") for n in (1, 2, 3)]
        assert 590 <= await redis.ttl(key.redis_key()) <= 600
        stranger = DialogueKey(key.tenant_id, uuid4(), key.conversation_id)
        assert await store.load(stranger) == []
    finally:
        await redis.delete(key.redis_key())
        await redis.aclose()


async def test_redis_failure_is_store_unavailable() -> None:
    redis = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    store = RedisDialogueStore(redis)
    key = DialogueKey(uuid4(), uuid4(), uuid4())
    try:
        with pytest.raises(DialogueStoreUnavailableError):
            await store.load(key)
        with pytest.raises(DialogueStoreUnavailableError):
            await store.append(key, Turn("q", "a"), keep=3, ttl_seconds=60)
    finally:
        await redis.aclose()
