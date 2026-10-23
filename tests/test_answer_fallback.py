"""Запасная модель в сервисе ответов (BH-50).

Ответ резерва сотрудник видит как обычный; в журнале — кто ответил и
почему резерв, в метрике — этап и причина. При открытом предохранителе
уточнение по истории и ночные подписи пробелов модель не зовут.
"""

from collections.abc import AsyncGenerator
from dataclasses import replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.metrics import FAQ_DEGRADED, LLM_FALLBACK
from corp_ed.domain.models import QaLog, Tenant, User
from corp_ed.llm.errors import LLMError
from corp_ed.llm.fallback import CircuitBreaker, FallbackGateway
from corp_ed.llm.gateway import LLMGateway
from corp_ed.llm.types import Completion, Message
from corp_ed.prompts.dialogue import Turn
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.services.gap_report_service import PLACEHOLDER_TITLE
from tests.test_gap_report import ask, clusters_of, service, topic
from tests.test_search_step import (
    QUESTION,
    QUESTION_AXIS,
    ScriptedStream,
    _document,
    _service,
)

ANSWER = "Закупку согласует директор [1]."


class Named(ScriptedStream):
    """ScriptedStream с именем поставщика в Completion."""

    def __init__(self, provider: str, *answers: list[str] | str) -> None:
        super().__init__(*answers)
        self.provider = provider

    def _completion(self, content: str) -> Completion:
        return replace(super()._completion(content), provider=self.provider)


class Silent(LLMGateway):
    """Flash, который не ответил в срок."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
        response_format: dict[str, Any] | None = None,
    ) -> Completion:
        self.calls += 1
        raise LLMError("no answer in 8 s", retryable=True, kind="timeout")

    async def stream(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1000,
    ) -> AsyncGenerator[str | Completion, None]:
        self.calls += 1
        raise LLMError("no answer in 8 s", retryable=True, kind="timeout")
        yield ""  # pragma: no cover


def _opened() -> CircuitBreaker:
    breaker = CircuitBreaker(failures=3, open_seconds=120)
    breaker.record_failure("error_config")
    return breaker


async def _log(session: AsyncSession) -> QaLog:
    return (await session.scalars(select(QaLog))).one()


def _count(stage: str, reason: str) -> float:
    return LLM_FALLBACK.labels(stage, reason)._value.get()


async def test_flash_timeout_answer_comes_from_reserve(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    await _document(
        session, "Регламент", ["Закупки согласует директор."], QUESTION_AXIS
    )
    primary = Silent()
    reserve = Named("cloudru", ANSWER)
    gateway = FallbackGateway(primary, reserve, CircuitBreaker())
    before = _count("answer", "timeout")

    result = await _service(chunk_repo, gateway).answer_turn(
        QUESTION, employee, history=[]
    )

    assert result.content == ANSWER
    assert result.answer_given is True
    assert primary.calls == 1
    assert _count("answer", "timeout") == before + 1
    entry = await _log(session)
    assert (entry.answer_provider, entry.answer_fallback_reason) == (
        "cloudru",
        "timeout",
    )


async def test_flash_answer_is_logged_without_reason(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    await _document(
        session, "Регламент", ["Закупки согласует директор."], QUESTION_AXIS
    )
    reserve = Named("cloudru")
    gateway = FallbackGateway(Named("yandex", ANSWER), reserve, CircuitBreaker())

    await _service(chunk_repo, gateway).answer_turn(QUESTION, employee, history=[])

    assert reserve.calls == []
    entry = await _log(session)
    assert (entry.answer_provider, entry.answer_fallback_reason) == ("yandex", None)


async def test_general_answer_from_reserve_is_counted_as_general(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    # Выдержек нет — общий ответ; предохранитель открыт — сразу резерв.
    reserve = Named("cloudru", "В документах компании ответа нет.\nПариж.")
    gateway = FallbackGateway(Silent(), reserve, _opened())
    before = _count("general", "breaker_open")

    await _service(chunk_repo, gateway).answer_turn(
        "Какая столица Франции?", employee, history=[]
    )

    assert _count("general", "breaker_open") == before + 1
    entry = await _log(session)
    assert entry.answer_fallback_reason == "breaker_open"


async def test_condense_is_skipped_while_the_breaker_is_open(
    session: AsyncSession,
    chunk_repo: ChunkRepository,
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    await _document(
        session, "Регламент", ["Закупки согласует директор."], QUESTION_AXIS
    )
    primary = Silent()
    reserve = Named("cloudru", ANSWER)
    gateway = FallbackGateway(primary, reserve, _opened())
    skipped = _count("condense", "breaker_open")
    degraded = FAQ_DEGRADED.labels("condense")._value.get()

    result = await _service(chunk_repo, gateway).answer_turn(
        QUESTION, employee, history=[Turn("Есть ли регламент закупок?", "Да.")]
    )

    # Один вызов — ответ резервом; переписывания не было, искали по вопросу.
    assert result.content == ANSWER
    assert len(reserve.calls) == 1
    assert primary.calls == 0
    assert _count("condense", "breaker_open") == skipped + 1
    assert FAQ_DEGRADED.labels("condense")._value.get() == degraded + 1
    assert (await _log(session)).standalone_question == QUESTION


async def test_gap_labels_wait_for_the_next_night_while_open(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    ask(session, employee, "Как подключить VPN?", topic(1))
    ask(session, employee, "VPN не работает", topic(1))
    await session.commit()
    primary = Silent()
    before = _count("gaps_label", "breaker_open")

    [report] = await service(
        session_maker, FallbackGateway(primary, None, _opened())
    ).run("test")

    assert report.labeled == 0
    assert primary.calls == 0
    assert _count("gaps_label", "breaker_open") == before + 1
    [cluster] = await clusters_of(session, tenant_ctx.id)
    assert cluster.title in (
        PLACEHOLDER_TITLE,
        "Как подключить VPN?",
        "VPN не работает",
    )
    assert cluster.prompt_version == ""
