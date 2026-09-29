"""Ночной отчёт о пробелах (BH-21, BH-22): классы, кластеры, подписи."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.cli import _parser
from corp_ed.core.config import EMBEDDING_DIM, GapsSettings
from corp_ed.core.exceptions import ConflictError
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import (
    GapCluster,
    GapClusterQuestion,
    QaLog,
    Tenant,
    User,
    UserRole,
)
from corp_ed.llm.errors import LLMError
from corp_ed.llm.fake import FakeAdapter
from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.types import Completion, FinishReason, Message, Usage
from corp_ed.prompts.gaps import GAP_LABEL_SCHEMA, PROMPT_VERSION
from corp_ed.services import gap_report_service
from corp_ed.services.gap_report_service import GapReportService
from corp_ed.services.retention_service import RetentionService

MODEL = "text-embeddings-v2-query@768"
LABEL = json.dumps({"title": "Командировки", "missing": "Нет положения"})
SETTINGS = GapsSettings(cluster_distance=0.6, half_life_days=14)


def topic(axis: int) -> list[float]:
    """Вектор темы: ось axis. Вопросы одной темы — расстояние 0, разных — 1."""
    vector = [0.0] * EMBEDDING_DIM
    vector[axis] = 1.0
    return vector


def ask(
    session: AsyncSession,
    user: User,
    question: str,
    vector: list[float],
    *,
    distance: float | None = 0.9,
    answer_given: bool = False,
    feedback: int | None = None,
    days_ago: float = 1,
    model: str = MODEL,
) -> QaLog:
    """Строка журнала: по умолчанию — вопрос без ответа (вектор мимо порога)."""
    entry = QaLog(
        tenant_id=user.tenant_id,
        user_id=user.id,
        question=question,
        question_embedding=vector,
        embedding_model=model,
        prompt_version="p",
        llm_model="m",
        best_vector_distance=distance,
        answer_given=answer_given,
        origin="documents" if answer_given else "general_knowledge",
        feedback=feedback,
        created_at=datetime.now(UTC) - timedelta(days=days_ago),
    )
    session.add(entry)
    return entry


async def colleague(session: AsyncSession, tenant: Tenant, email: str) -> User:
    user = User(
        tenant_id=tenant.id,
        email=email,
        role=UserRole.EMPLOYEE,
        hashed_password="x",
    )
    session.add(user)
    await session.commit()
    return user


class LabelLLM(LLMGateway):
    """Подпись по первому вопросу группы — чтобы различать кластеры."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[list[Message]] = []
        self.formats: list[dict[str, Any] | None] = []
        self.fail = fail

    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
        response_format: dict[str, Any] | None = None,
    ) -> Completion:
        self.calls.append(messages)
        self.formats.append(response_format)
        if self.fail:
            raise LLMError("недоступен", retryable=True)
        first = messages[-1].content.splitlines()[1].removeprefix("1. ")
        return Completion(
            content=json.dumps({"title": f"Про {first}", "missing": "Нет документа"}),
            finish_reason=FinishReason.COMPLETED,
            usage=Usage(input_tokens=0, output_tokens=0),
            model_version="fake",
            model="fake",
            latency_ms=0,
        )


def service(
    session_maker: async_sessionmaker[AsyncSession],
    llm: LLMGateway | None = None,
    settings: GapsSettings = SETTINGS,
) -> GapReportService:
    return GapReportService(
        session_maker, llm or FakeAdapter(content=LABEL), settings, max_distance=0.5
    )


async def clusters_of(session: AsyncSession, tenant_id: UUID) -> list[GapCluster]:
    with tenant_scope(tenant_id):
        result = await session.execute(
            select(GapCluster)
            .order_by(GapCluster.priority.desc())
            .execution_options(populate_existing=True)
        )
        clusters = list(result.scalars())
        await session.commit()
    return clusters


async def members_of(session: AsyncSession, cluster: GapCluster) -> set[UUID]:
    with tenant_scope(cluster.tenant_id):
        result = await session.execute(
            select(GapClusterQuestion.qa_log_id).where(
                GapClusterQuestion.cluster_id == cluster.id
            )
        )
        ids = set(result.scalars())
        await session.commit()
    return ids


# --- классы и кластеры ------------------------------------------------------------


async def test_unanswered_questions_are_grouped_by_topic(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    other = await colleague(session, tenant_ctx, "b@test.com")
    trips = [
        ask(session, employee, "Как оформить командировку?", topic(0)),
        ask(session, other, "Какие суточные в командировке?", topic(0)),
        ask(session, employee, "Командировка за границу?", topic(0)),
    ]
    vpn = [
        ask(session, employee, "Как подключить VPN?", topic(1)),
        ask(session, employee, "VPN не работает", topic(1)),
    ]
    await session.commit()

    [report] = await service(session_maker).run("test")

    assert (report.window, report.candidates, report.clusters) == (5, 5, 2)
    first, second = await clusters_of(session, tenant_ctx.id)
    # Командировки: больше вопросов и два человека — выше приоритет.
    assert await members_of(session, first) == {q.id for q in trips}
    assert (first.question_count, first.user_count) == (3, 2)
    assert await members_of(session, second) == {q.id for q in vpn}
    assert first.priority > second.priority
    assert first.status == "new"
    assert first.title == "Командировки"
    assert first.missing == "Нет положения"
    assert first.prompt_version == PROMPT_VERSION
    assert first.embedding_model == MODEL


async def test_only_gaps_and_model_refusals_are_reported(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    gap = ask(session, employee, "Пробел", topic(0))
    refusal = ask(session, employee, "Отказ модели", topic(0), distance=0.3)
    answered = ask(
        session, employee, "Ответили", topic(0), distance=0.3, answer_given=True
    )
    disliked = ask(
        session,
        employee,
        "Ответили не то",
        topic(0),
        distance=0.3,
        answer_given=True,
        feedback=-1,
    )
    await session.commit()

    await service(session_maker).run("test")

    [cluster] = await clusters_of(session, tenant_ctx.id)
    assert await members_of(session, cluster) == {gap.id, refusal.id}
    with tenant_scope(tenant_ctx.id):
        kinds = dict(
            (
                await session.execute(
                    select(QaLog.id, QaLog.miss_kind).execution_options(
                        populate_existing=True
                    )
                )
            ).all()
        )
        await session.commit()
    assert kinds == {
        gap.id: "gap",
        refusal.id: "model_refusal",
        answered.id: "answered",
        disliked.id: "retrieval_miss",
    }


async def test_single_question_is_not_a_topic(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    ask(session, employee, "Один вопрос", topic(0))
    await session.commit()

    [report] = await service(session_maker).run("test")

    assert report.clusters == 0
    assert await clusters_of(session, tenant_ctx.id) == []


async def test_questions_outside_window_or_of_old_model_are_ignored(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    ask(session, employee, "Свежий", topic(0))
    ask(session, employee, "Свежий 2", topic(0))
    ask(session, employee, "Давний", topic(0), days_ago=40)
    ask(session, employee, "Старая модель", topic(0), days_ago=2, model="old@256")
    await session.commit()

    await service(session_maker).run("test")

    [cluster] = await clusters_of(session, tenant_ctx.id)
    assert cluster.question_count == 2


# --- изоляция компаний ------------------------------------------------------------


async def test_companies_are_clustered_separately(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    """Одинаковые векторы двух компаний — два отчёта, не один кластер."""
    other = Tenant(id=uuid4(), company_code="other", name="Other")
    session.add(other)
    await session.commit()
    with tenant_scope(other.id):
        stranger = await colleague(session, other, "s@other.ru")
        foreign = [
            ask(session, stranger, "Чужой вопрос 1", topic(0)),
            ask(session, stranger, "Чужой вопрос 2", topic(0)),
        ]
        await session.commit()
    ours = [
        ask(session, employee, "Наш вопрос 1", topic(0)),
        ask(session, employee, "Наш вопрос 2", topic(0)),
    ]
    await session.commit()

    reports = await service(session_maker).run()

    assert sorted(r.company_code for r in reports) == ["other", "test"]
    [our_cluster] = await clusters_of(session, tenant_ctx.id)
    [their_cluster] = await clusters_of(session, other.id)
    assert await members_of(session, our_cluster) == {q.id for q in ours}
    assert await members_of(session, their_cluster) == {q.id for q in foreign}
    assert their_cluster.tenant_id == other.id


async def test_suspended_company_is_skipped(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
) -> None:
    tenant_ctx.is_active = False
    await session.commit()

    assert await service(session_maker).run() == []


# --- подписи ------------------------------------------------------------------------


async def test_label_uses_strict_json_and_masked_questions(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    ask(session, employee, "Пишу на ivan@corp.ru про отпуск", topic(0))
    ask(session, employee, "Отпуск, звонить +7 915 123-45-67", topic(0))
    await session.commit()
    llm = LabelLLM()

    await service(session_maker, llm).run("test")

    assert llm.formats == [GAP_LABEL_SCHEMA]
    prompt = " ".join(m.content for m in llm.calls[0])
    assert "ivan@corp.ru" not in prompt
    assert "915" not in prompt


async def test_label_failure_leaves_placeholder_and_retries_next_night(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    ask(session, employee, "Как оформить командировку?", topic(0), days_ago=2)
    ask(session, employee, "Суточные?", topic(0))
    await session.commit()

    [report] = await service(session_maker, LabelLLM(fail=True)).run("test")

    assert report.failed is False
    assert report.labeled == 0
    [cluster] = await clusters_of(session, tenant_ctx.id)
    assert cluster.title == "Суточные?"  # самый свежий вопрос
    assert cluster.prompt_version == ""

    await service(session_maker, LabelLLM()).run("test")

    [cluster] = await clusters_of(session, tenant_ctx.id)
    assert cluster.title == "Про Суточные?"
    assert cluster.prompt_version == PROMPT_VERSION


async def test_label_count_per_run_is_capped(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    for axis in (0, 1):
        ask(session, employee, f"Тема {axis} вопрос 1", topic(axis))
        ask(session, employee, f"Тема {axis} вопрос 2", topic(axis))
    await session.commit()
    llm = LabelLLM()
    capped = GapsSettings(cluster_distance=0.6, half_life_days=14, max_labels_per_run=1)

    [report] = await service(session_maker, llm, capped).run("test")

    assert report.clusters == 2
    assert report.labeled == 1
    assert len(llm.calls) == 1


async def test_model_text_is_cleaned_and_bounded(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    ask(session, employee, "Вопрос 1", topic(0))
    ask(session, employee, "Вопрос 2", topic(0))
    await session.commit()
    noisy = json.dumps({"title": "Тема\n\x00" + "я" * 500, "missing": "a\tb\x1bc"})

    await service(session_maker, FakeAdapter(content=noisy)).run("test")

    [cluster] = await clusters_of(session, tenant_ctx.id)
    assert len(cluster.title) == 200
    assert "\n" not in cluster.title and "\x00" not in cluster.title
    assert cluster.missing == "a b c"


# --- повторные запуски ---------------------------------------------------------------


async def test_rebuild_keeps_id_status_and_label(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    """Ночная пересборка не сбрасывает то, что сделал админ, и не
    подписывает кластер заново (название не прыгает, LLM не платим)."""
    ask(session, employee, "Командировка 1", topic(0))
    ask(session, employee, "Командировка 2", topic(0))
    await session.commit()
    await service(session_maker).run("test")
    [before] = await clusters_of(session, tenant_ctx.id)
    with tenant_scope(tenant_ctx.id):
        await session.execute(
            update(GapCluster)
            .where(GapCluster.id == before.id)
            .values(status="in_progress")
        )
        await session.commit()

    new_question = ask(session, employee, "Командировка 3", topic(0))
    await session.commit()
    llm = LabelLLM()
    await service(session_maker, llm).run("test")

    [after] = await clusters_of(session, tenant_ctx.id)
    assert after.id == before.id
    assert after.status == "in_progress"
    assert after.title == "Командировки"
    assert after.question_count == 3
    assert new_question.id in await members_of(session, after)
    assert llm.calls == []


async def test_vanished_cluster_is_removed(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    ask(session, employee, "Вопрос 1", topic(0))
    ask(session, employee, "Вопрос 2", topic(0))
    await session.commit()
    await service(session_maker).run("test")

    later = GapReportService(
        session_maker,
        FakeAdapter(content=LABEL),
        SETTINGS,
        max_distance=0.5,
        now=lambda: datetime.now(UTC) + timedelta(days=60),
    )
    await later.run("test")

    assert await clusters_of(session, tenant_ctx.id) == []


async def test_second_concurrent_run_is_refused(
    session_maker: async_sessionmaker[AsyncSession], tenant_ctx: Tenant
) -> None:
    async with session_maker() as holder:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": gap_report_service._LOCK_KEY},
        )
        with pytest.raises(ConflictError):
            await service(session_maker).run("test")
        await holder.rollback()


async def test_unknown_company_is_an_error(
    session_maker: async_sessionmaker[AsyncSession], tenant_ctx: Tenant
) -> None:
    with pytest.raises(ConflictError):
        await service(session_maker).run("ghost")


async def test_purge_removes_expired_questions_from_clusters(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    """Срок хранения сильнее отчёта: удалённый вопрос уходит и из состава
    кластера (каскад работает и под RLS)."""
    old = ask(session, employee, "Старый", topic(0), days_ago=29)
    ask(session, employee, "Новый", topic(0))
    await session.commit()
    await service(session_maker).run("test")

    await RetentionService(session_maker, qa_log_days=10).purge()

    [cluster] = await clusters_of(session, tenant_ctx.id)
    assert old.id not in await members_of(session, cluster)


def test_cli_gaps_needs_scope() -> None:
    assert _parser().parse_args(["gaps", "--all"]).all is True
    assert _parser().parse_args(["gaps", "--code", "acme"]).code == "acme"
    with pytest.raises(SystemExit):
        _parser().parse_args(["gaps"])
    with pytest.raises(SystemExit):
        _parser().parse_args(["gaps", "--all", "--code", "acme"])
