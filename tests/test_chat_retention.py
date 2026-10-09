"""Срок хранения диалогов чата (cli purge): настройка компании, по
умолчанию 12 месяцев.

Диалог удаляется целиком — с сообщениями, вложениями (их текстом по
фрагментам) и общей ссылкой, — когда его последняя активность старше
срока своей компании. Срок другой компании на него не влияет.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import (
    ChatAttachment,
    ChatAttachmentChunk,
    ChatMessage,
    Conversation,
    Tenant,
    User,
)
from corp_ed.services.retention_service import RetentionService, months_before
from tests.factories import make_user

NOW = datetime(2026, 10, 9, 3, 10, tzinfo=UTC)


@pytest.mark.parametrize(
    ("moment", "months", "expected"),
    [
        (datetime(2026, 10, 9, 3, 10, tzinfo=UTC), 1, datetime(2026, 9, 9, 3, 10)),
        (datetime(2026, 10, 9, 3, 10, tzinfo=UTC), 12, datetime(2025, 10, 9, 3, 10)),
        # Через границу года.
        (datetime(2026, 2, 15, tzinfo=UTC), 3, datetime(2025, 11, 15)),
        (datetime(2026, 1, 31, tzinfo=UTC), 36, datetime(2023, 1, 31)),
        # Такого числа в прошлом месяце нет — последний день месяца.
        (datetime(2026, 3, 31, tzinfo=UTC), 1, datetime(2026, 2, 28)),
        (datetime(2028, 3, 31, tzinfo=UTC), 1, datetime(2028, 2, 29)),
        (datetime(2026, 12, 31, tzinfo=UTC), 6, datetime(2026, 6, 30)),
    ],
)
def test_months_before_counts_calendar_months(
    moment: datetime, months: int, expected: datetime
) -> None:
    assert months_before(moment, months) == expected.replace(tzinfo=UTC)


async def _conversation(
    session: AsyncSession,
    owner: User,
    *,
    created_at: datetime,
    updated_at: datetime,
    shared: bool = False,
) -> Conversation:
    """Диалог с вопросом, ответом и вложением к вопросу (с фрагментами)."""
    with tenant_scope(owner.tenant_id):
        conversation = Conversation(
            id=uuid4(),
            user_id=owner.id,
            title="Отпуск",
            created_at=created_at,
            updated_at=updated_at,
            share_token=uuid4().hex if shared else None,
        )
        session.add(conversation)
        await session.flush()
        attachment = ChatAttachment(
            id=uuid4(),
            user_id=owner.id,
            conversation_id=conversation.id,
            filename="договор.pdf",
            source_format="pdf",
            size=1,
            tokens=10,
            created_at=created_at,
        )
        session.add(attachment)
        await session.flush()
        session.add_all(
            [
                ChatAttachmentChunk(
                    attachment_id=attachment.id, position=i, content=f"Пункт {i}"
                )
                for i in range(3)
            ]
        )
        question = ChatMessage(
            id=uuid4(),
            conversation_id=conversation.id,
            role="user",
            content="Сколько дней отпуска?",
            attachment_ids=[attachment.id],
            created_at=updated_at,
        )
        session.add(question)
        await session.flush()
        answer = ChatMessage(
            id=uuid4(),
            conversation_id=conversation.id,
            parent_id=question.id,
            role="assistant",
            content="28 дней.",
            sources=[{"chunk_id": str(uuid4()), "content": "Отпуск — 28 дней."}],
            created_at=updated_at + timedelta(microseconds=1),
        )
        session.add(answer)
        conversation.current_message_id = answer.id
        if shared:
            conversation.shared_message_id = answer.id
            conversation.shared_at = updated_at
        await session.commit()
        return conversation


async def _ids(session: AsyncSession, column: Any) -> set[UUID]:
    return set((await session.scalars(select(column))).all())


async def _left(session: AsyncSession, tenant_id: UUID) -> dict[str, set[UUID]]:
    """Что осталось у компании: диалоги; диалоги, у которых есть сообщения
    и вложения; вложения и вложения, у которых есть фрагменты."""
    with tenant_scope(tenant_id):
        return {
            "conversations": await _ids(session, Conversation.id),
            "messages": await _ids(session, ChatMessage.conversation_id),
            "attachments": await _ids(session, ChatAttachment.conversation_id) - {None},
            "attachment_ids": await _ids(session, ChatAttachment.id),
            "chunks": await _ids(session, ChatAttachmentChunk.attachment_id),
        }


async def test_purge_removes_dialogs_inactive_longer_than_company_term(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    # Компания A хранит диалоги месяц, компания B — три года.
    tenant_ctx.chat_retention_months = 1
    other = Tenant(
        id=uuid4(), company_code="other", name="Other Co", chat_retention_months=36
    )
    session.add(other)
    await session.commit()
    stranger = make_user(tenant_id=other.id, email="stranger@other.com")
    with tenant_scope(other.id):
        session.add(stranger)
        await session.commit()

    # A: последняя активность 40 дней назад — старше месяца, удаляется
    # вместе с вложением, фрагментами и общей ссылкой.
    stale = await _conversation(
        session,
        employee,
        created_at=NOW - timedelta(days=400),
        updated_at=NOW - timedelta(days=40),
        shared=True,
    )
    # A: начат давно, но вопрос задан 10 дней назад — жив. Считается
    # последняя активность, а не дата начала.
    active = await _conversation(
        session,
        employee,
        created_at=NOW - timedelta(days=400),
        updated_at=NOW - timedelta(days=10),
    )
    # A: файл загружен, вопрос ещё не отправлен — это не диалог, его
    # срок — сутки, и он тут свежий.
    with tenant_scope(tenant_ctx.id):
        pending = ChatAttachment(
            id=uuid4(),
            user_id=employee.id,
            filename="черновик.docx",
            source_format="docx",
            size=1,
            tokens=1,
            created_at=NOW - timedelta(hours=1),
        )
        session.add(pending)
        await session.commit()
    # B: те же 40 дней — срок B три года, жив.
    same_age = await _conversation(
        session,
        stranger,
        created_at=NOW - timedelta(days=40),
        updated_at=NOW - timedelta(days=40),
    )
    # B: дольше трёх лет без активности — удаляется по сроку B.
    ancient = await _conversation(
        session,
        stranger,
        created_at=NOW - timedelta(days=4 * 365),
        updated_at=NOW - timedelta(days=4 * 365),
    )
    with tenant_scope(tenant_ctx.id):
        stale_attachment = (
            await session.scalars(
                select(ChatAttachment.id).where(
                    ChatAttachment.conversation_id == stale.id
                )
            )
        ).one()

    report = await RetentionService(session_maker, qa_log_days=365).purge(now=NOW)

    assert report.conversations == 2
    session.expunge_all()
    left_a = await _left(session, tenant_ctx.id)
    assert left_a["conversations"] == {active.id}
    assert left_a["messages"] == {active.id}
    assert left_a["attachments"] == {active.id}
    # Вложение удалённого диалога ушло вместе с фрагментами; черновик жив.
    assert stale_attachment not in left_a["attachment_ids"]
    assert stale_attachment not in left_a["chunks"]
    assert pending.id in left_a["attachment_ids"]
    assert left_a["chunks"] == left_a["attachment_ids"] - {pending.id}
    with tenant_scope(tenant_ctx.id):
        assert (
            await session.scalars(
                select(Conversation).where(
                    Conversation.share_token == stale.share_token
                )
            )
        ).first() is None

    left_b = await _left(session, other.id)
    assert left_b["conversations"] == {same_age.id}
    assert left_b["messages"] == {same_age.id}
    assert left_b["attachments"] == {same_age.id}
    assert ancient.id not in left_b["messages"]
    assert len(left_b["chunks"]) == 1


async def test_new_company_keeps_dialogs_twelve_months(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
    employee: User,
) -> None:
    await session.refresh(tenant_ctx)
    assert tenant_ctx.chat_retention_months == 12

    # 9 октября − 12 месяцев = 9 октября прошлого года, 03:10.
    kept = await _conversation(
        session,
        employee,
        created_at=datetime(2025, 10, 9, 3, 11, tzinfo=UTC),
        updated_at=datetime(2025, 10, 9, 3, 11, tzinfo=UTC),
    )
    gone = await _conversation(
        session,
        employee,
        created_at=datetime(2025, 10, 9, 3, 9, tzinfo=UTC),
        updated_at=datetime(2025, 10, 9, 3, 9, tzinfo=UTC),
    )

    report = await RetentionService(session_maker, qa_log_days=365).purge(now=NOW)

    assert report.conversations == 1
    session.expunge_all()
    left = await _left(session, tenant_ctx.id)
    assert left["conversations"] == {kept.id}
    assert gone.id not in left["messages"]
