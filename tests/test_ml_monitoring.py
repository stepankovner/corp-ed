"""Мониторинг ML без схемы решения (BH-49, ml-monitoring.md № 17–19).

Ближайшая дистанция — в гистограмму, оценки — в счётчик, время ответа —
в журнал, версии промптов и моделей — одной строкой Info.
"""

from prometheus_client import REGISTRY
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.config import LLMSettings, RagSettings
from corp_ed.core.metrics import FAQ_FEEDBACK, FEEDBACK_REASON_LABELS, FEEDBACK_VOTES
from corp_ed.domain.models import QaLog, Tenant, User
from corp_ed.prompts.decision import DECISION_PROMPT_VERSION
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.services.ml_build import ml_build
from tests.test_search_step import (
    QUESTION,
    QUESTION_AXIS,
    ScriptedStream,
    _document,
    _service,
)


def _distance_count() -> float:
    value = REGISTRY.get_sample_value("corp_ed_faq_best_distance_count")
    return value or 0.0


async def test_answer_logs_latency_and_observes_the_nearest_distance(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    await _document(
        session, "Регламент", ["Закупки согласует директор."], QUESTION_AXIS
    )
    before = _distance_count()

    await _service(chunk_repo, ScriptedStream("Директор [1].")).answer_turn(
        QUESTION, employee, history=[]
    )

    entry = (await session.scalars(select(QaLog))).one()
    assert entry.latency_ms is not None and entry.latency_ms >= 0
    assert _distance_count() == before + 1


async def test_rating_counts_the_vote(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    faq = _service(chunk_repo, ScriptedStream("В документах компании ответа нет."))
    answer = await faq.answer_turn("Что-то вне документов?", employee, history=[])
    assert answer.log_id is not None
    before = FAQ_FEEDBACK.labels("down", "none")._value.get()

    await faq.rate(employee, answer.log_id, -1)

    assert FAQ_FEEDBACK.labels("down", "none")._value.get() == before + 1


def test_feedback_labels_come_from_the_lists() -> None:
    for family in REGISTRY.collect():
        if family.name != "corp_ed_faq_feedback":
            continue
        for sample in family.samples:
            assert sample.labels["vote"] in FEEDBACK_VOTES
            assert sample.labels["feedback_reason"] in FEEDBACK_REASON_LABELS


def test_ml_build_lists_prompt_and_model_versions() -> None:
    llm = LLMSettings(  # type: ignore[call-arg]
        llm_provider="yandex-openai", yc_folder_id="f", yc_api_key="k"
    )
    rag = RagSettings(  # type: ignore[call-arg]
        context_max_tokens=3000,
        faq_limit=5,
        faq_max_distance=0.59,
        faq_temperature=0,
        retriever="vector",
        chunk_tokens=400,
        overlap_tokens=50,
        fulltext_weight=0.5,
        faq_search_step=True,
        faq_small_talk=True,
    )

    build = ml_build(llm, rag)

    assert build["decision_prompt"] == DECISION_PROMPT_VERSION
    assert build["faq_prompt"] == "faq-v2.9"
    assert build["general_prompt"] == "faq-v2.10"
    assert build["answer_model"] == llm.llm_model
