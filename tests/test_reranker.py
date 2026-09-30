"""Реранкер (M3, Р-14): архитектура за выключенным флагом.

Модель выбирает и включает ML; здесь — что пайплайн делает с баллами,
как переживает сбой сервиса и что пишет в журнал.
"""

import asyncio
import json
from collections.abc import Sequence
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.config import EMBEDDING_DIM, RagSettings
from corp_ed.core.exceptions import ConflictError, ServiceUnavailableError
from corp_ed.domain.models import Chunk, Material, QaLog, User
from corp_ed.domain.rerank import RerankText, rerank_order, rerank_passage
from corp_ed.domain.types import AnswerOrigin, ChunkMatch, Retriever
from corp_ed.llm.fake import FakeAdapter
from corp_ed.llm.fake_embedding import FakeEmbeddingAdapter
from corp_ed.llm.reranker import FakeReranker, HttpReranker, Reranker, RerankerError
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.repositories.glossary_repository import GlossaryRepository
from corp_ed.repositories.qa_log_repository import QaLogRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.services.faq_service import FaqService
from tests.conftest import make_credit_service

QUESTION = "Какой размер гранта по программе УМНИК?"


def _match(n: int, text: str, embed: str = "") -> ChunkMatch:
    return ChunkMatch(
        id=uuid4(),
        content=text,
        material_id=uuid4(),
        position=n,
        distance=0.1 * n,
        title="Док",
        heading_path=[],
        embed_text=embed,
    )


# --- чистые функции -------------------------------------------------------------


def test_order_is_by_score_and_stable_on_ties() -> None:
    a, b, c = _match(1, "a"), _match(2, "b"), _match(3, "c")

    assert rerank_order([a, b, c], [0.1, 0.9, 0.1]) == [b, a, c]
    with pytest.raises(ValueError):
        rerank_order([a], [])


def test_passage_is_embed_text_like_the_ml_measure() -> None:
    match = _match(1, "текст для промпта", embed="Док > Раздел\nтекст")

    assert rerank_passage(match, RerankText.EMBED) == "Док > Раздел\nтекст"
    assert rerank_passage(match, RerankText.LLM) == "текст для промпта"
    # Старый чанк без embed_text — текст промпта, а не пустая строка.
    assert rerank_passage(_match(2, "x"), RerankText.EMBED) == "x"


def test_reranker_is_off_by_default() -> None:
    fields = RagSettings.model_fields
    assert fields["reranker"].default == "off"
    assert fields["rerank_depth"].default == 30


# --- клиент сервиса (контракт text-embeddings-inference) -------------------------


def _http(handler: object) -> HttpReranker:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return HttpReranker(client, "http://reranker:8080/", model="m", timeout=1.0)


async def test_http_reranker_contract() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        # Сервис отдаёт по убыванию балла — клиент возвращает по порядку.
        return httpx.Response(
            200, json=[{"index": 1, "score": 0.9}, {"index": 0, "score": 0.2}]
        )

    scores = await _http(handler).score("вопрос", ["первый", "второй"])

    assert scores == [0.2, 0.9]
    assert str(seen[0].url) == "http://reranker:8080/rerank"
    assert json.loads(seen[0].content) == {
        "query": "вопрос",
        "texts": ["первый", "второй"],
        "truncate": True,
    }


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503),
        httpx.Response(200, json={"error": "x"}),
        httpx.Response(200, json=[{"index": 0, "score": 0.5}]),
        httpx.Response(200, content=b"not json"),
    ],
    ids=["status", "shape", "missing", "garbage"],
)
async def test_http_reranker_rejects_bad_answers(response: httpx.Response) -> None:
    with pytest.raises(RerankerError):
        await _http(lambda request: response).score("q", ["a", "b"])


async def test_http_reranker_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(RerankerError):
        await _http(handler).score("q", ["a"])


# --- в ответе ---------------------------------------------------------------------


class SlowReranker(Reranker):
    model = "slow"

    async def score(self, query: str, passages: Sequence[str]) -> list[float]:
        await asyncio.sleep(1)
        return [0.0] * len(passages)


class BrokenReranker(Reranker):
    model = "broken"

    async def score(self, query: str, passages: Sequence[str]) -> list[float]:
        raise RerankerError("HTTP 503")


def _service(
    session: AsyncSession,
    reranker: Reranker | None,
    llm: FakeAdapter,
    *,
    limit: int = 2,
    depth: int = 30,
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
        limit=limit,
        max_distance=0.6,
        context_max_tokens=3000,
        temperature=0.0,
        retriever=Retriever.VECTOR,
        fulltext_weight=0.5,
        reranker=reranker,
        rerank_depth=depth,
        rerank_timeout=0.05,
    )


@pytest.fixture
async def chunks(session: AsyncSession, material: Material) -> list[str]:
    """Три фрагмента на одном расстоянии; нужный — последним в порядке
    вектора, реранкер (слова вопроса) ставит его первым."""
    texts = [
        "Суточные по России — 700 рублей.",
        "Отпуск — 28 календарных дней.",
        "Размер гранта по программе УМНИК — 500 тыс. рублей.",
    ]
    for position, text in enumerate(texts):
        session.add(
            Chunk(
                material_id=material.id,
                position=position,
                heading_path=[],
                embed_text=f"Документ\n{text}",
                content=text,
                embedding=[0.1] * EMBEDDING_DIM,
                model="fake",
                model_version="fake",
            )
        )
    await session.commit()
    return texts


async def test_reranker_picks_the_best_excerpts(
    session: AsyncSession, chunks: list[str], employee: User
) -> None:
    reranker = FakeReranker()
    llm = FakeAdapter(content="500 тыс. рублей [1].")

    result = await _service(session, reranker, llm, limit=2).answer(QUESTION, employee)

    assert result.origin is AnswerOrigin.DOCUMENTS
    # Реранкер видел всех прошедших порог (глубина 30 ≥ 3) и embed_text.
    assert reranker.calls[0][0] == QUESTION
    assert sorted(reranker.calls[0][1]) == sorted(f"Документ\n{t}" for t in chunks)
    assert result.sources[0].content == chunks[2]
    assert len(result.sources) == 2
    assert result.sources[0].rerank_score is not None
    prompt = llm.calls[0][-1].content
    assert prompt.index("УМНИК — 500") < prompt.index("Суточные")
    assert result.diagnostics is not None
    assert result.diagnostics.rerank_model == "fake-reranker"
    entry = await session.scalar(select(QaLog).where(QaLog.id == result.log_id))
    assert entry is not None
    assert entry.rerank_model == "fake-reranker"


@pytest.mark.parametrize(
    "reranker", [SlowReranker(), BrokenReranker()], ids=["timeout", "error"]
)
async def test_reranker_failure_keeps_vector_order(
    session: AsyncSession, chunks: list[str], employee: User, reranker: Reranker
) -> None:
    result = await _service(session, reranker, FakeAdapter(), limit=2).answer(
        QUESTION, employee
    )

    assert result.origin is AnswerOrigin.DOCUMENTS
    assert len(result.sources) == 2
    assert all(source.rerank_score is None for source in result.sources)
    assert result.diagnostics is not None
    assert result.diagnostics.rerank_model is None
    assert result.diagnostics.rerank_ms is not None


async def test_without_reranker_nothing_changes(
    session: AsyncSession, chunks: list[str], employee: User
) -> None:
    result = await _service(session, None, FakeAdapter(), limit=2).answer(
        QUESTION, employee
    )

    assert len(result.sources) == 2
    assert result.diagnostics is not None
    assert result.diagnostics.rerank_model is None
    assert result.diagnostics.rerank_ms is None


async def test_search_debug_returns_rerank_scores(
    session: AsyncSession, chunks: list[str], admin: User
) -> None:
    service = _service(session, FakeReranker(), FakeAdapter())

    matches = await service.search(QUESTION, 2, viewer=admin, rerank=True)

    assert [m.content for m in matches][0] == chunks[2]
    assert all(m.rerank_score is not None for m in matches)


async def test_search_debug_refuses_rerank_when_off(
    session: AsyncSession, chunks: list[str], admin: User
) -> None:
    with pytest.raises(ConflictError):
        await _service(session, None, FakeAdapter()).search(
            QUESTION, 2, viewer=admin, rerank=True
        )
    with pytest.raises(ServiceUnavailableError):
        await _service(session, BrokenReranker(), FakeAdapter()).search(
            QUESTION, 2, viewer=admin, rerank=True
        )
