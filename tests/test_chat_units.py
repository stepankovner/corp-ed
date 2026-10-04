"""Чат (ТЗ §6): название диалога, что показывается в потоке, выдержки
большого вложения."""

from uuid import uuid4

import pytest
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.config import EMBEDDING_DIM
from corp_ed.domain.models import ChatAttachment, ChatAttachmentChunk, Tenant, User
from corp_ed.prompts.faq import GENERAL_ANSWER_PREFIX
from corp_ed.repositories.chat_repository import MessageRepository
from corp_ed.services.chat_generation import (
    ATTACHMENT_TOP_K,
    ERROR_MESSAGES,
    AttachmentContext,
    ChatEvent,
    ChatGenerator,
    ErrorEvent,
    GenerationJob,
    InMemoryStopSignals,
)
from corp_ed.services.chat_service import make_title
from corp_ed.services.faq_service import FaqService, _GeneralPrefixGate, _RefusalGate


def test_title_is_first_line_cut_at_word_boundary() -> None:
    assert make_title("  Сколько   дней отпуска?\nподробнее") == "Сколько дней отпуска?"
    long = "Как оформить командировку в другой город, если поездка выпадает на выходные"
    title = make_title(long)
    assert title.endswith("…")
    assert len(title) <= 61
    assert long.startswith(title[:-1])
    assert make_title("   ") == "Новый диалог"


def _feed(gate: _RefusalGate | _GeneralPrefixGate, pieces: list[str]) -> str:
    return "".join(gate.feed(piece) for piece in pieces)


def test_refusal_gate_holds_refusal_and_passes_answers() -> None:
    assert _feed(_RefusalGate(), ["В докум", "ентах компании ", "ответа нет."]) == ""
    assert _feed(_RefusalGate(), ["«В документах", " компании ответа нет»"]) == ""
    assert _feed(_RefusalGate(), ["В докум", "ентах сказано: 28 дней [1]."]) == (
        "В документах сказано: 28 дней [1]."
    )
    assert _feed(_RefusalGate(), ["Отпуск", " — 28 дней."]) == "Отпуск — 28 дней."


def test_general_gate_drops_service_prefix_keeps_spacing() -> None:
    text = f"{GENERAL_ANSWER_PREFIX}\nОбычно отпуск длится 28 календарных дней, "
    shown = _feed(_GeneralPrefixGate(), [text[:30], text[30:], "но бывает иначе."])
    assert shown == "Обычно отпуск длится 28 календарных дней, но бывает иначе."

    one_line = (
        "В документах компании ответа нет. Ниже — общая информация, не из "
        "документов компании: к сожалению, точного ответа нет, проверьте у HR."
    )
    assert _feed(_GeneralPrefixGate(), [one_line]).startswith("К сожалению")


async def test_large_attachment_sends_nearest_fragments_in_text_order(
    session: AsyncSession, tenant_ctx: Tenant, employee: User
) -> None:
    attachment = ChatAttachment(
        id=uuid4(),
        user_id=employee.id,
        filename="договор.pdf",
        source_format="pdf",
        size=1,
        tokens=10_000,
    )
    session.add(attachment)
    await session.flush()
    near, far = (
        [1.0] + [0.0] * (EMBEDDING_DIM - 1),
        [0.0, 1.0] + [0.0] * (EMBEDDING_DIM - 2),
    )
    for position in range(30):
        session.add(
            ChatAttachmentChunk(
                attachment_id=attachment.id,
                position=position,
                content=f"Пункт {position}. " + "текст " * 200,
                embedding=near if position % 3 == 0 else far,
            )
        )
    await session.commit()

    context = AttachmentContext(session, [attachment.id])
    selected = await context.select(near)

    assert 0 < len(selected) <= ATTACHMENT_TOP_K
    positions = [match.position for match in selected]
    assert positions == sorted(positions)
    assert all(position % 3 == 0 for position in positions)
    assert {match.title for match in selected} == {"договор.pdf"}
    assert context.selected == selected


async def test_small_attachment_goes_whole(
    session: AsyncSession, tenant_ctx: Tenant, employee: User
) -> None:
    attachment = ChatAttachment(
        id=uuid4(),
        user_id=employee.id,
        filename="памятка.txt",
        source_format="txt",
        size=1,
        tokens=10,
    )
    session.add(attachment)
    await session.flush()
    for position in range(3):
        session.add(
            ChatAttachmentChunk(
                attachment_id=attachment.id,
                position=position,
                content=f"Часть {position}",
            )
        )
    await session.commit()

    selected = await AttachmentContext(session, [attachment.id]).select(
        [0.1] * EMBEDDING_DIM
    )
    assert [m.content for m in selected] == ["Часть 0", "Часть 1", "Часть 2"]


def _job(tenant: Tenant) -> GenerationJob:
    return GenerationJob(
        tenant_id=tenant.id,
        member_id=uuid4(),
        conversation_id=uuid4(),
        answer_id=uuid4(),
        question="Сколько дней отпуска?",
        history=[],
        attachment_ids=[],
        diagnostics=False,
    )


def _generator(session_maker: async_sessionmaker[AsyncSession]) -> ChatGenerator:
    def build_faq(_: AsyncSession) -> FaqService:
        raise AssertionError("до FaqService не доходит: сотрудника нет")

    return ChatGenerator(session_maker, build_faq, InMemoryStopSignals())


async def test_stream_ends_when_even_the_failure_cannot_be_saved(
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """База не отдала соединение и для записи ошибки — поток всё равно
    получает итог, а не «пинг» вечно (docs/LOAD-TEST.md)."""

    async def pool_exhausted(*_: object) -> None:
        raise SQLAlchemyTimeoutError("QueuePool limit reached")

    monkeypatch.setattr(MessageRepository, "get", pool_exhausted)
    events: list[ChatEvent] = []

    await _generator(session_maker).run(_job(tenant_ctx), events.append)

    assert events == [ErrorEvent("internal", ERROR_MESSAGES["internal"], None)]


async def test_saved_failure_is_the_only_final_event(
    session_maker: async_sessionmaker[AsyncSession], tenant_ctx: Tenant
) -> None:
    events: list[ChatEvent] = []

    await _generator(session_maker).run(_job(tenant_ctx), events.append)

    # Сообщения нет — ошибка без него; второго итога нет.
    assert events == [ErrorEvent("internal", ERROR_MESSAGES["internal"], None)]
