"""Чат (ТЗ §6): название диалога, что показывается в потоке, выдержки
большого вложения."""

from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.config import EMBEDDING_DIM
from corp_ed.domain.models import ChatAttachment, ChatAttachmentChunk, Tenant, User
from corp_ed.prompts.faq import GENERAL_ANSWER_PREFIX
from corp_ed.services.chat_generation import (
    ATTACHMENT_TOP_K,
    AttachmentContext,
)
from corp_ed.services.chat_service import make_title
from corp_ed.services.faq_service import _GeneralPrefixGate, _RefusalGate


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
