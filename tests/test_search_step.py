"""Второй шаг поиска — ответ из двух документов (BH-43, промпт faq-v2.9).

Вопрос «Кто согласует закупку?» находит регламент с должностью, но не
оргструктуру с именем: модель пишет «Дополнительный поиск: …», сервис ищет
по её запросу и спрашивает снова — новые выдержки после прежних.
"""

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.config import EMBEDDING_DIM
from corp_ed.domain.models import Chunk, Material, QaLog, Tenant, User
from corp_ed.domain.types import AnswerOrigin, NotFoundMode, Retriever
from corp_ed.llm.fake_embedding import FakeEmbeddingAdapter
from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.types import Completion, EmbeddingResult, FinishReason, Message, Usage
from corp_ed.prompts.faq import PROMPT_VERSION, SEARCH_PROMPT_VERSION
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.repositories.glossary_repository import GlossaryRepository
from corp_ed.repositories.qa_log_repository import QaLogRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.services.faq_service import (
    FaqService,
    _RefusalGate,
    _SearchGate,
)
from corp_ed.services.general_answer import REFUSAL_ANSWER
from tests.conftest import make_credit_service

QUESTION = "Кто согласует закупку на 2 млн?"
QUERY = "финансовый директор ФИО"
REQUEST = f"Дополнительный поиск: {QUERY}"
ANSWER = "Согласует финансовый директор [1] — Петров Иван Сергеевич [2]."


def _axis(index: int) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    vector[index] = 1.0
    return vector


QUESTION_AXIS = _axis(0)
QUERY_AXIS = _axis(1)
OTHER_AXIS = _axis(2)


class KeyedEmbeddings(FakeEmbeddingAdapter):
    """Вопрос и запрос второго шага — разные векторы: каждый находит свой
    документ, чужой — на расстоянии 1 (дальше порога)."""

    async def embed_query(self, text: str) -> EmbeddingResult:
        self.query_calls.append(text)
        vector = {QUESTION: QUESTION_AXIS, QUERY: QUERY_AXIS}.get(text, OTHER_AXIS)
        return EmbeddingResult(
            embedding=vector,
            input_tokens=0,
            model_version="fake",
            model=f"fake@{EMBEDDING_DIM}",
            latency_ms=0,
        )


class ScriptedStream(LLMGateway):
    """Ответы по очереди, каждый — кусками потока; 100 токенов на входе и
    10 на выходе за вызов."""

    def __init__(self, *answers: list[str] | str) -> None:
        self.answers = [[a] if isinstance(a, str) else a for a in answers]
        self.calls: list[list[Message]] = []

    def _completion(self, content: str) -> Completion:
        return Completion(
            content=content,
            finish_reason=FinishReason.COMPLETED,
            usage=Usage(input_tokens=100, output_tokens=10),
            model_version="fake",
            model="fake",
            latency_ms=0,
        )

    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
        response_format: dict[str, Any] | None = None,
    ) -> Completion:
        self.calls.append(messages)
        return self._completion("".join(self.answers[len(self.calls) - 1]))

    async def stream(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
    ) -> AsyncGenerator[str | Completion, None]:
        self.calls.append(messages)
        pieces = self.answers[len(self.calls) - 1]
        for piece in pieces:
            yield piece
        yield self._completion("".join(pieces))


class RecordingSink:
    def __init__(self, stop_before_piece: int | None = None) -> None:
        self.stages: list[str] = []
        self.deltas: list[str] = []
        self.resets = 0
        self._asked = 0
        self._stop_at = stop_before_piece

    async def stage(self, stage: str) -> None:
        self.stages.append(stage)

    async def origin(self, origin: AnswerOrigin) -> None:
        return None

    async def delta(self, text: str) -> None:
        self.deltas.append(text)

    async def reset(self) -> None:
        self.resets += 1

    async def should_stop(self) -> bool:
        self._asked += 1
        return self._stop_at is not None and self._asked > self._stop_at


def _service(
    chunk_repo: ChunkRepository, llm: LLMGateway, *, search_step: bool = True
) -> FaqService:
    return FaqService(
        chunk_repo=chunk_repo,
        qa_log_repo=QaLogRepository(chunk_repo.session),
        tenant_repo=TenantRepository(chunk_repo.session),
        glossary_repo=GlossaryRepository(chunk_repo.session),
        credits=make_credit_service(chunk_repo.session),
        embedding_gateway=KeyedEmbeddings(),
        llm_gateway=llm,
        session=chunk_repo.session,
        limit=5,
        max_distance=0.6,
        context_max_tokens=3000,
        temperature=0.0,
        retriever=Retriever.VECTOR,
        fulltext_weight=0.5,
        search_step=search_step,
    )


async def _document(
    session: AsyncSession, title: str, texts: list[str], axis: list[float]
) -> list[Chunk]:
    material = Material(title=title, content="\n".join(texts))
    session.add(material)
    await session.flush()
    chunks = [
        Chunk(
            material_id=material.id,
            position=position,
            content=text,
            embed_text=text,
            embedding=axis,
            model="fake",
            model_version="fake",
        )
        for position, text in enumerate(texts)
    ]
    session.add_all(chunks)
    await session.commit()
    return chunks


async def _two_documents(session: AsyncSession) -> tuple[Chunk, Chunk]:
    [rule] = await _document(
        session,
        "Регламент закупок",
        ["Закупки свыше 1 млн согласует финансовый директор."],
        QUESTION_AXIS,
    )
    [person] = await _document(
        session,
        "Оргструктура",
        ["Финансовый директор — Петров Иван Сергеевич."],
        QUERY_AXIS,
    )
    return rule, person


def _text(messages: list[Message]) -> str:
    return "\n".join(m.content for m in messages)


async def _strict(session: AsyncSession, tenant: Tenant) -> None:
    tenant.not_found_mode = NotFoundMode.STRICT.value
    await session.commit()


async def _log(session: AsyncSession) -> QaLog:
    return (await session.scalars(select(QaLog))).one()


async def test_second_search_adds_new_excerpt_after_previous(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    rule, person = await _two_documents(session)
    llm = ScriptedStream(REQUEST, ANSWER)
    service = _service(chunk_repo, llm)
    sink = RecordingSink()

    result = await service.answer_turn(QUESTION, employee, history=[], sink=sink)

    assert result.content == ANSWER
    assert result.origin is AnswerOrigin.DOCUMENTS
    # Прежняя выдержка — [1], новая — [2]: номера в ответе совпадают.
    assert [s.id for s in result.sources] == [rule.id, person.id]
    assert service.embedding_gateway.query_calls == [QUESTION, QUERY]  # type: ignore[attr-defined]

    first, second = llm.calls
    assert "Дополнительный поиск" in _text(first)
    assert "Петров" not in _text(first)
    assert "Дополнительный поиск уже сделан" in _text(second)
    assert _text(second).index("финансовый директор.") < _text(second).index("Петров")

    # Строку запроса сотрудник не видит; этап второго поиска — видит.
    assert "".join(sink.deltas) == ANSWER
    assert sink.stages == ["searching", "writing", "searching_more", "writing"]

    entry = await _log(session)
    assert entry.prompt_version == SEARCH_PROMPT_VERSION
    assert entry.search_query == QUERY
    assert entry.source_chunk_ids == [rule.id, person.id]
    # Оба вызова модели — в токенах и кредитах.
    assert (entry.input_tokens, entry.output_tokens) == (200, 20)
    assert entry.credits == service.credits.cost(220)
    assert result.diagnostics is not None
    assert result.diagnostics.prompt_version == SEARCH_PROMPT_VERSION


async def test_request_is_hidden_from_stream_whatever_the_pieces(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    await _two_documents(session)
    llm = ScriptedStream(
        ["«Дополнит", "ельный  поиск", ": финансовый ", "директор ФИО»"],
        ["Согласует ", "финансовый директор [1]."],
    )
    sink = RecordingSink()

    result = await _service(chunk_repo, llm).answer_turn(
        QUESTION, employee, history=[], sink=sink
    )

    assert sink.deltas == ["Согласует ", "финансовый директор [1]."]
    assert result.content == "Согласует финансовый директор [1]."


async def test_nothing_new_is_a_refusal(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    # Оргструктуры нет: по запросу находится только уже показанная выдержка
    # (или ничего) — ответа по документам нет, второго вызова нет.
    await _strict(session, tenant_ctx)
    await _document(
        session,
        "Регламент закупок",
        ["Закупки свыше 1 млн согласует финансовый директор."],
        QUESTION_AXIS,
    )
    llm = ScriptedStream(REQUEST)
    sink = RecordingSink()

    result = await _service(chunk_repo, llm).answer_turn(
        QUESTION, employee, history=[], sink=sink
    )

    assert len(llm.calls) == 1
    assert result.content == REFUSAL_ANSWER
    assert result.origin is AnswerOrigin.NONE
    assert result.sources == []
    assert sink.deltas == []
    entry = await _log(session)
    assert entry.search_query == QUERY
    assert entry.answer_given is False
    # Вызов модели был — он оплачивается.
    assert entry.input_tokens == 100


async def test_repeated_request_is_a_refusal(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    await _strict(session, tenant_ctx)
    await _two_documents(session)
    llm = ScriptedStream(REQUEST, "Дополнительный поиск: приказ о назначении")
    sink = RecordingSink()

    result = await _service(chunk_repo, llm).answer_turn(
        QUESTION, employee, history=[], sink=sink
    )

    # Один раз на вопрос: третьего вызова и второго поиска нет.
    assert len(llm.calls) == 2
    assert result.content == REFUSAL_ANSWER
    assert result.origin is AnswerOrigin.NONE
    assert sink.deltas == []
    assert (await _log(session)).input_tokens == 200


async def test_empty_request_is_a_refusal_without_search(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    await _strict(session, tenant_ctx)
    await _two_documents(session)
    llm = ScriptedStream("Дополнительный поиск:")
    service = _service(chunk_repo, llm)

    result = await service.answer_turn(QUESTION, employee, history=[])

    assert result.content == REFUSAL_ANSWER
    assert service.embedding_gateway.query_calls == [QUESTION]  # type: ignore[attr-defined]
    assert (await _log(session)).search_query is None


async def test_at_most_three_new_and_eight_excerpts(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    first = await _document(
        session, "Регламент", [f"Правило {n}." for n in range(5)], QUESTION_AXIS
    )
    more = await _document(
        session, "Оргструктура", [f"Сотрудник {n}." for n in range(5)], QUERY_AXIS
    )
    llm = ScriptedStream(REQUEST, ANSWER)

    result = await _service(chunk_repo, llm).answer_turn(QUESTION, employee, history=[])

    assert len(result.sources) == 8
    assert {s.id for s in result.sources[:5]} == {c.id for c in first}
    assert {s.id for s in result.sources[5:]} <= {c.id for c in more}
    assert "[8]" in _text(llm.calls[1])
    assert "[9]" not in _text(llm.calls[1])


async def test_stop_during_request_keeps_no_request_text(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    await _two_documents(session)
    llm = ScriptedStream(["Дополнительный поиск:", " финансовый директор"])
    sink = RecordingSink(stop_before_piece=1)

    result = await _service(chunk_repo, llm).answer_turn(
        QUESTION, employee, history=[], sink=sink
    )

    assert result.stopped is True
    assert result.content == ""
    assert sink.deltas == []


async def test_search_step_off_keeps_faq_v27(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    await _two_documents(session)
    llm = ScriptedStream("Согласует финансовый директор [1].")

    await _service(chunk_repo, llm, search_step=False).answer_turn(
        QUESTION, employee, history=[]
    )

    assert "Дополнительный поиск" not in _text(llm.calls[0])
    entry = await _log(session)
    assert entry.prompt_version == PROMPT_VERSION
    assert entry.search_query is None


# --- строка запроса после ответа (BH-44) --------------------------------------

AFTER_ANSWER = [
    # той же строкой, фраза разрезана между кусками
    (
        [
            "Наставника назначает ",
            "директор по персоналу [1]. Допол",
            "нительный поиск: ФИО директора",
        ],
        "Наставника назначает директор по персоналу [1].",
    ),
    # следующей строкой
    (
        ["Согласует финансовый директор [1].", "\n", "Дополнительный поиск: ФИО"],
        "Согласует финансовый директор [1].",
    ),
    # жирным
    (
        ["Согласует финансовый директор [1].\n**Дополни", "тельный поиск:** ФИО"],
        "Согласует финансовый директор [1].",
    ),
]


@pytest.mark.parametrize(
    ("pieces", "answer"), AFTER_ANSWER, ids=["same-line", "next-line", "bold"]
)
async def test_request_after_the_answer_is_cut_without_second_search(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
    pieces: list[str],
    answer: str,
) -> None:
    rule, _ = await _two_documents(session)
    llm = ScriptedStream(pieces)
    service = _service(chunk_repo, llm)
    sink = RecordingSink()

    result = await service.answer_turn(QUESTION, employee, history=[], sink=sink)

    shown = "".join(sink.deltas)
    assert "оиск" not in shown
    assert shown.rstrip() == answer
    assert result.content == answer
    assert result.origin is AnswerOrigin.DOCUMENTS
    assert [s.id for s in result.sources] == [rule.id]
    # Ответ уже показан — второго поиска и второго вызова нет.
    assert len(llm.calls) == 1
    assert service.embedding_gateway.query_calls == [QUESTION]  # type: ignore[attr-defined]
    assert "searching_more" not in sink.stages
    assert (await _log(session)).search_query is None


async def test_answer_with_the_word_search_is_untouched(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    await _two_documents(session)
    answer = (
        "Поиск поставщика — по регламенту закупок [1].\n"
        "Дополнительно согласует финансовый директор [1]."
    )
    cut = answer.index("Дополни") + len("Дополни")
    llm = ScriptedStream([answer[:20], answer[20:cut], answer[cut:]])
    sink = RecordingSink()

    result = await _service(chunk_repo, llm).answer_turn(
        QUESTION, employee, history=[], sink=sink
    )

    assert "".join(sink.deltas) == answer
    assert result.content == answer


async def test_second_answer_with_a_request_at_the_end_is_cut(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    # После второго поиска модель ответила и снова дописала запрос: это не
    # повторная просьба (ответ есть) — строку срезаем, ответ показываем.
    rule, person = await _two_documents(session)
    llm = ScriptedStream(
        REQUEST, ANSWER + "\nДополнительный поиск: приказ о назначении"
    )
    sink = RecordingSink()

    result = await _service(chunk_repo, llm).answer_turn(
        QUESTION, employee, history=[], sink=sink
    )

    assert result.content == ANSWER
    assert [s.id for s in result.sources] == [rule.id, person.id]
    assert "".join(sink.deltas).rstrip() == ANSWER
    assert len(llm.calls) == 2


async def test_stop_on_a_request_after_the_answer_keeps_the_answer(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    await _two_documents(session)
    llm = ScriptedStream(
        ["Согласует директор [1]. ", "Дополнительный поиск: ФИО", " директора"]
    )
    sink = RecordingSink(stop_before_piece=2)

    result = await _service(chunk_repo, llm).answer_turn(
        QUESTION, employee, history=[], sink=sink
    )

    assert result.stopped is True
    assert result.content == "Согласует директор [1]."
    assert "оиск" not in "".join(sink.deltas)


def _feed(pieces: list[str]) -> list[str]:
    gate = _SearchGate(_RefusalGate())
    return [gate.feed(piece) for piece in pieces]


def test_gate_passes_ordinary_answer_at_once() -> None:
    assert _feed(["По регламенту ", "согласует директор [1]."]) == [
        "По регламенту ",
        "согласует директор [1].",
    ]


def test_gate_holds_only_while_answer_may_be_a_request() -> None:
    # «Дополнительно…» начинается как запрос — придержали, потом отдали целиком.
    assert _feed(["Доп", "олнительно ", "оплачивается [1]."]) == [
        "",
        "Дополнительно ",
        "оплачивается [1].",
    ]
    assert _feed(["Дополнительный поиск", ": X", "\nещё"]) == ["", "", ""]


def test_gate_holds_a_tail_that_may_become_a_request() -> None:
    # BH-44: после ответа держим только хвост, который может оказаться
    # началом строки запроса; дописалась — дальше ничего.
    assert _feed(["Ответ [1]. Доп", "олнительный ", "поиск: X", " ещё"]) == [
        "Ответ [1]. ",
        "",
        "",
        "",
    ]
    assert _feed(["Ответ [1]. Доп", "олнительно — тоже [2]."]) == [
        "Ответ [1]. ",
        "Дополнительно — тоже [2].",
    ]
