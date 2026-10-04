"""Реранкер (M3, BH-32): за флагом, по умолчанию выключен.

Правило перестановки — ML (domain.rerank, tests/test_rerank_domain.py);
здесь — что пайплайн ответа делает с баллами модели: кого ей показывает,
что уходит в промпт, как переживает сбой сервиса и что пишет в журнал.
"""

import asyncio
import json
import re
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.config import EMBEDDING_DIM, RagSettings
from corp_ed.core.exceptions import ConflictError, ServiceUnavailableError
from corp_ed.core.tenant_context import current_tenant
from corp_ed.domain.models import Chunk, Material, QaLog, Tenant, User
from corp_ed.domain.types import AnswerOrigin, ChunkMatch, Retriever
from corp_ed.llm.fake import FakeAdapter
from corp_ed.llm.fake_embedding import FakeEmbeddingAdapter
from corp_ed.llm.reranker import (
    FakeReranker,
    HttpReranker,
    Reranker,
    RerankerError,
    rerank_passage,
)
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.repositories.glossary_repository import GlossaryRepository
from corp_ed.repositories.qa_log_repository import QaLogRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.services.faq_service import FaqService
from tests.conftest import make_credit_service

QUESTION = "Какой размер гранта по программе УМНИК?"
NEAR = [0.1] * EMBEDDING_DIM
"""Вектор вопроса у FakeEmbeddingAdapter: расстояние 0."""
OPPOSITE = [-0.1] * EMBEDDING_DIM
"""Расстояние 2 — за любым порогом."""


def _match(text: str, embed: str = "") -> ChunkMatch:
    return ChunkMatch(
        id=uuid4(),
        content=text,
        material_id=uuid4(),
        position=0,
        distance=0.1,
        title="Док",
        heading_path=[],
        embed_text=embed,
    )


# --- настройки и текст пары --------------------------------------------------------


def test_reranker_is_off_by_default_with_the_contract_settings() -> None:
    fields = RagSettings.model_fields
    assert fields["rerank_model"].default == ""
    assert fields["rerank_depth"].default == 30
    assert fields["rerank_max_length"].default == 512
    assert fields["rerank_timeout_ms"].default == 3000
    assert fields["rerank_max_words"].default == 24


def test_empty_max_words_is_no_limit() -> None:
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
    empty = RagSettings(**(base | {"rerank_max_words": ""}))  # type: ignore[arg-type]
    assert empty.rerank_max_words is None
    twelve = RagSettings(**(base | {"rerank_max_words": "12"}))  # type: ignore[arg-type]
    assert twelve.rerank_max_words == 12


def test_depth_fits_the_service_batch() -> None:
    # Больше --max-client-batch-size сервис не примет — и ответ тихо уйдёт
    # в порядок вектора. Настройка и compose.yaml должны совпадать.
    compose = (Path(__file__).parents[1] / "compose.yaml").read_text()
    batch = re.search(r'"--max-client-batch-size", "(\d+)"', compose)
    assert batch is not None
    limits = RagSettings.model_fields["rerank_depth"].metadata
    assert [getattr(m, "le", None) for m in limits if hasattr(m, "le")] == [
        int(batch.group(1))
    ]


def test_pair_text_is_embed_text_like_the_ml_measure() -> None:
    assert rerank_passage(_match("текст", embed="Док > Раздел\nтекст")) == (
        "Док > Раздел\nтекст"
    )
    # Чанк до крошек без embed_text — текст для промпта, а не пустая строка.
    assert rerank_passage(_match("x")) == "x"


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
    max_words: int | None = 24,
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
        max_distance=0.59,
        context_max_tokens=3000,
        temperature=0.0,
        retriever=Retriever.VECTOR,
        fulltext_weight=0.5,
        reranker=reranker,
        rerank_depth=depth,
        rerank_timeout=0.05,
        rerank_max_words=max_words,
    )


def _chunk(material: Material, position: int, text: str, vector: list[float]) -> Chunk:
    return Chunk(
        material_id=material.id,
        position=position,
        heading_path=[],
        embed_text=f"Документ\n{text}",
        content=text,
        embedding=vector,
        model="fake",
        model_version="fake",
    )


TEXTS = [
    "Суточные по России — 700 рублей.",
    "Отпуск — 28 календарных дней.",
    "Размер гранта по программе УМНИК — 500 тыс. рублей.",
]


@pytest.fixture
async def chunks(session: AsyncSession, material: Material) -> list[str]:
    """Три фрагмента на одном расстоянии; нужный — последним в порядке
    вектора, реранкер (слова вопроса) ставит его первым."""
    for position, text in enumerate(TEXTS):
        session.add(_chunk(material, position, text, NEAR))
    await session.commit()
    return TEXTS


async def test_reranker_picks_the_best_excerpts(
    session: AsyncSession, chunks: list[str], employee: User
) -> None:
    reranker = FakeReranker()
    llm = FakeAdapter(content="500 тыс. рублей [1].")

    result = await _service(session, reranker, llm, limit=2).answer(QUESTION, employee)

    assert result.origin is AnswerOrigin.DOCUMENTS
    # Модель видела всех прошедших порог (глубина 30 ≥ 3) — пары с embed_text.
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
    assert entry.rerank_ms is not None


async def test_excerpts_past_the_threshold_never_reach_the_model(
    session: AsyncSession, material: Material, employee: User
) -> None:
    session.add_all(
        [
            _chunk(material, 0, "Отпуск — 28 календарных дней.", NEAR),
            _chunk(material, 1, "Грант УМНИК — 500 тыс. рублей.", NEAR),
            # Слова вопроса есть, но вектор далеко: за порогом 0,59.
            _chunk(material, 2, "Размер гранта по УМНИК — 400 тыс.", OPPOSITE),
        ]
    )
    await session.commit()
    reranker = FakeReranker()
    llm = FakeAdapter(content="Ответ [1].")

    result = await _service(session, reranker, llm, limit=5).answer(QUESTION, employee)

    shown = reranker.calls[0][1]
    assert len(shown) == 2
    assert not any("400 тыс." in passage for passage in shown)
    assert all("400 тыс." not in source.content for source in result.sources)
    assert "400 тыс." not in llm.calls[0][-1].content


async def test_pool_holds_only_excerpts_the_employee_may_see(
    session: AsyncSession,
    tenant_ctx: Tenant,
    material: Material,
    employee: User,
) -> None:
    secret = Material(title="Только отделу", content="x", visibility="restricted")
    session.add(secret)
    await session.flush()
    session.add_all(
        [
            _chunk(material, 0, "Отпуск — 28 календарных дней.", NEAR),
            _chunk(material, 1, "Грант УМНИК — 500 тыс. рублей.", NEAR),
            _chunk(secret, 0, "Размер гранта по программе УМНИК — тайна.", NEAR),
        ]
    )
    await session.commit()
    # Чужая компания с тем же вектором: RLS не пускает её в поиск.
    foreign = Tenant(id=uuid4(), company_code="other-rerank", name="Other Co")
    session.add(foreign)
    await session.commit()
    current_tenant.set(foreign.id)
    foreign_material = Material(
        id=uuid4(), tenant_id=foreign.id, title="Чужой", content="x"
    )
    session.add(foreign_material)
    await session.flush()
    session.add(_chunk(foreign_material, 0, "Грант УМНИК чужой компании.", NEAR))
    await session.commit()
    current_tenant.set(tenant_ctx.id)
    reranker = FakeReranker()

    await _service(session, reranker, FakeAdapter(), limit=5).answer(QUESTION, employee)

    shown = reranker.calls[0][1]
    assert len(shown) == 2
    assert not any("тайна" in passage or "чужой" in passage for passage in shown)


async def test_depth_limits_what_the_model_sees(
    session: AsyncSession, chunks: list[str], employee: User
) -> None:
    reranker = FakeReranker()

    await _service(session, reranker, FakeAdapter(), limit=2, depth=2).answer(
        QUESTION, employee
    )

    assert len(reranker.calls[0][1]) == 2


async def test_single_candidate_is_not_sent_to_the_model(
    session: AsyncSession, material: Material, employee: User
) -> None:
    session.add(_chunk(material, 0, TEXTS[2], NEAR))
    await session.commit()
    reranker = FakeReranker()

    result = await _service(session, reranker, FakeAdapter()).answer(QUESTION, employee)

    assert reranker.calls == []
    assert len(result.sources) == 1
    assert result.diagnostics is not None
    assert result.diagnostics.rerank_model is None


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
    assert [s.content for s in result.sources] == chunks[:2]
    assert all(source.rerank_score is None for source in result.sources)
    assert result.diagnostics is not None
    assert result.diagnostics.rerank_model is None
    assert result.diagnostics.rerank_ms is not None
    entry = await session.scalar(select(QaLog).where(QaLog.id == result.log_id))
    assert entry is not None
    assert entry.rerank_model is None
    assert entry.rerank_ms is not None


async def test_without_reranker_nothing_changes(
    session: AsyncSession, chunks: list[str], employee: User
) -> None:
    llm = FakeAdapter()

    result = await _service(session, None, llm, limit=2).answer(QUESTION, employee)

    assert [s.content for s in result.sources] == chunks[:2]
    assert result.diagnostics is not None
    assert result.diagnostics.rerank_model is None
    assert result.diagnostics.rerank_ms is None
    entry = await session.scalar(select(QaLog).where(QaLog.id == result.log_id))
    assert entry is not None
    assert (entry.rerank_model, entry.rerank_ms) == (None, None)


async def test_search_debug_returns_the_answer_order_with_scores(
    session: AsyncSession, material: Material, admin: User
) -> None:
    session.add_all(
        [
            _chunk(material, 0, TEXTS[0], NEAR),
            _chunk(material, 1, TEXTS[2], NEAR),
            _chunk(material, 2, "Грант УМНИК — 400 тыс.", OPPOSITE),
        ]
    )
    await session.commit()
    service = _service(session, FakeReranker(), FakeAdapter())

    matches = await service.search(QUESTION, 3, viewer=admin, rerank=True)

    # Прошедшие порог — по баллу, за порогом — следом и без балла: порог
    # в отладке не отсекает, а показывает, как упорядочил бы ответ.
    assert [m.content for m in matches][:2] == [TEXTS[2], TEXTS[0]]
    assert matches[2].content == "Грант УМНИК — 400 тыс."
    assert matches[2].rerank_score is None
    assert all(m.rerank_score is not None for m in matches[:2])


async def test_search_debug_refuses_rerank_when_it_cannot(
    session: AsyncSession, chunks: list[str], admin: User
) -> None:
    with pytest.raises(ConflictError):
        await _service(session, None, FakeAdapter()).search(
            QUESTION, 2, viewer=admin, rerank=True
        )
    with pytest.raises(ConflictError):
        await _service(session, FakeReranker(), FakeAdapter()).search(
            QUESTION, 2, retriever=Retriever.HYBRID, viewer=admin, rerank=True
        )
    with pytest.raises(ServiceUnavailableError):
        await _service(session, BrokenReranker(), FakeAdapter()).search(
            QUESTION, 2, viewer=admin, rerank=True
        )


# --- длинные вопросы (BH-40) ------------------------------------------------------

LONG = (
    "Добрый день, я третий год работаю в отделе продаж, у меня появилась идея "
    "своего проекта, коллеги советуют подать заявку, поэтому хочу уточнить: "
    "какой размер гранта по программе УМНИК?"
)


def test_long_question_is_longer_than_the_limit() -> None:
    assert len(LONG.split()) > 24


async def test_long_question_keeps_vector_order(
    session: AsyncSession, chunks: list[str], employee: User
) -> None:
    """Длиннее 24 слов — реранкер не зовём, как будто он выключен."""
    reranker = FakeReranker()
    llm = FakeAdapter(content="Ответ [1].")

    result = await _service(session, reranker, llm).answer(LONG, employee)

    assert reranker.calls == []
    assert [s.content for s in result.sources] == chunks[:2]
    assert all(s.rerank_score is None for s in result.sources)
    entry = await session.scalar(select(QaLog).where(QaLog.id == result.log_id))
    assert entry is not None
    assert (entry.rerank_model, entry.rerank_ms) == (None, None)


async def test_without_word_limit_long_questions_are_reranked(
    session: AsyncSession, chunks: list[str], employee: User
) -> None:
    reranker = FakeReranker()
    llm = FakeAdapter(content="Ответ [1].")

    result = await _service(session, reranker, llm, max_words=None).answer(
        LONG, employee
    )

    assert len(reranker.calls) == 1
    assert result.sources[0].content == chunks[2]


async def test_search_debug_follows_the_word_limit(
    session: AsyncSession, chunks: list[str], admin: User
) -> None:
    """/faq/search с rerank — порядок, который дал бы ответ."""
    reranker = FakeReranker()
    service = _service(session, reranker, FakeAdapter())

    matches = await service.search(LONG, 3, viewer=admin, rerank=True)

    assert reranker.calls == []
    assert [m.content for m in matches] == chunks
    assert all(m.rerank_score is None for m in matches)
