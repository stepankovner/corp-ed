"""Данные ушедшего из компании (cli purge): через 30 дней после ухода.

Ушёл сам, убрал администратор или человек удалил учётку — членство
остаётся строкой «ушёл» ради ссылок журналов, поэтому каскады внешних
ключей не срабатывают. Через MEMBER_DATA_RETENTION после left_at purge
удаляет его диалоги целиком, черновики вложений, уведомления, настройки
писем, подключения к своим системам (с токенами) и доступы к документам,
а в членстве стирает должность, отдел и служебные отметки. Журнал
вопросов и журнал действий живут по своим срокам.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.config import EMBEDDING_DIM
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import (
    ChatAttachment,
    ChatAttachmentChunk,
    ChatMessage,
    Connector,
    ConnectorUserGrant,
    Conversation,
    Department,
    Material,
    MaterialAccess,
    MemberStatus,
    Notification,
    NotificationSetting,
    QaLog,
    Tenant,
    User,
)
from corp_ed.services.retention_service import RetentionService
from tests.factories import make_user
from tests.test_chat_retention import _conversation

NOW = datetime(2026, 10, 9, 3, 10, tzinfo=UTC)


async def _member(
    session: AsyncSession,
    tenant: Tenant,
    email: str,
    *,
    left_days_ago: int | None,
    department: Department,
    connector: Connector,
    material: Material,
) -> User:
    """Член компании со всем, что у него бывает: профиль в компании,
    свежий диалог, черновик вложения, уведомление, настройки писем,
    подключение к своей системе и доступ к документу."""
    with tenant_scope(tenant.id):
        member = make_user(
            tenant_id=tenant.id,
            email=email,
            status=MemberStatus.ACTIVE if left_days_ago is None else MemberStatus.LEFT,
            left_at=None if left_days_ago is None else NOW - timedelta(left_days_ago),
            position="Бухгалтер",
            department_id=department.id,
            department_confirmed=True,
            last_login_at=NOW - timedelta(days=60),
            tips_seen_at=NOW - timedelta(days=60),
            checklist_hidden_at=NOW - timedelta(days=60),
        )
        session.add(member)
        await session.commit()
    # Диалог свежий: срок компании (12 месяцев) его не касается.
    await _conversation(
        session,
        member,
        created_at=NOW - timedelta(days=50),
        updated_at=NOW - timedelta(days=40),
        shared=True,
    )
    with tenant_scope(tenant.id):
        session.add_all(
            [
                ChatAttachment(
                    user_id=member.id,
                    filename="черновик.docx",
                    source_format="docx",
                    size=1,
                    tokens=1,
                    created_at=NOW - timedelta(hours=1),
                ),
                Notification(
                    tenant_id=tenant.id,
                    user_id=member.id,
                    kind="join_request",
                    title=f"Заявка: {email}",
                    body="",
                ),
                NotificationSetting(tenant_id=tenant.id, user_id=member.id),
                ConnectorUserGrant(
                    tenant_id=tenant.id,
                    connector_id=connector.id,
                    user_id=member.id,
                    credentials="шифротекст токена",
                ),
                MaterialAccess(
                    tenant_id=tenant.id, material_id=material.id, user_id=member.id
                ),
                QaLog(
                    tenant_id=tenant.id,
                    user_id=member.id,
                    question="Сколько дней отпуска?",
                    question_embedding=[0.0] * EMBEDDING_DIM,
                    embedding_model="test",
                    prompt_version="v1",
                    answer_given=True,
                    origin="documents",
                    created_at=NOW - timedelta(days=5),
                ),
            ]
        )
        await session.commit()
    return member


async def _left_of(session: AsyncSession, tenant_id: UUID, user_id: UUID) -> dict:
    """Что осталось от человека в компании."""
    session.expunge_all()
    with tenant_scope(tenant_id):

        async def count(model: type, column: object) -> int:
            rows = await session.scalars(select(model).where(column == user_id))  # type: ignore[operator]
            return len(rows.all())

        conversations = (
            await session.scalars(
                select(Conversation.id).where(Conversation.user_id == user_id)
            )
        ).all()
        member = (await session.scalars(select(User).where(User.id == user_id))).one()
        return {
            "conversations": len(conversations),
            "messages": len(
                (
                    await session.scalars(
                        select(ChatMessage.id).where(
                            ChatMessage.conversation_id.in_(conversations)
                        )
                    )
                ).all()
            ),
            "attachments": await count(ChatAttachment, ChatAttachment.user_id),
            "notifications": await count(Notification, Notification.user_id),
            "settings": await count(NotificationSetting, NotificationSetting.user_id),
            "grants": await count(ConnectorUserGrant, ConnectorUserGrant.user_id),
            "access": await count(MaterialAccess, MaterialAccess.user_id),
            "qa_log": await count(QaLog, QaLog.user_id),
            "profile": (
                member.position,
                member.department_id,
                member.department_confirmed,
                member.last_login_at,
                member.tips_seen_at,
                member.checklist_hidden_at,
            ),
            "status": member.status,
        }


async def test_purge_removes_data_of_members_who_left_30_days_ago(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
) -> None:
    with tenant_scope(tenant_ctx.id):
        department = Department(tenant_id=tenant_ctx.id, name="Бухгалтерия")
        connector = Connector(
            tenant_id=tenant_ctx.id, kind="yandex360", name="Диск", mode="per_user"
        )
        material = Material(
            tenant_id=tenant_ctx.id,
            title="Регламент",
            content="",
            visibility="restricted",
        )
        session.add_all([department, connector, material])
        await session.commit()
    common = {"department": department, "connector": connector, "material": material}
    gone = await _member(
        session, tenant_ctx, "gone@test.com", left_days_ago=31, **common
    )
    recent = await _member(
        session, tenant_ctx, "recent@test.com", left_days_ago=10, **common
    )
    working = await _member(
        session, tenant_ctx, "working@test.com", left_days_ago=None, **common
    )
    # Удалил учётку 40 дней назад: членство — «ушёл», без учётки.
    deleted = await _member(
        session, tenant_ctx, "deleted@test.com", left_days_ago=40, **common
    )
    with tenant_scope(tenant_ctx.id):
        assert deleted.account is not None
        await session.delete(deleted.account)
        await session.commit()

    report = await RetentionService(session_maker, qa_log_days=90).purge(now=NOW)

    assert report.left_members == 2
    for member in (gone, deleted):
        left = await _left_of(session, tenant_ctx.id, member.id)
        assert left == {
            "conversations": 0,
            "messages": 0,
            "attachments": 0,
            "notifications": 0,
            "settings": 0,
            "grants": 0,
            "access": 0,
            # Журнал вопросов — по своему сроку (90 дней).
            "qa_log": 1,
            "profile": (None, None, False, None, None, None),
            "status": MemberStatus.LEFT,
        }
    # Ушёл 10 дней назад — ещё может вернуться по приглашению; работающий
    # не тронут.
    for member in (recent, working):
        left = await _left_of(session, tenant_ctx.id, member.id)
        assert left["conversations"] == 1
        assert left["messages"] == 2
        assert left["attachments"] == 2
        assert left["notifications"] == left["settings"] == 1
        assert left["grants"] == left["access"] == 1
        assert left["profile"][:3] == ("Бухгалтер", department.id, True)

    # Фрагменты вложений ушедших — вместе с вложениями.
    with tenant_scope(tenant_ctx.id):
        attachments = set((await session.scalars(select(ChatAttachment.id))).all())
        chunk_owners = set(
            (await session.scalars(select(ChatAttachmentChunk.attachment_id))).all()
        )
    assert chunk_owners <= attachments

    # Второй прогон ничего не находит.
    again = await RetentionService(session_maker, qa_log_days=90).purge(now=NOW)
    assert again.left_members == 0


async def test_member_purge_stays_within_its_company(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
) -> None:
    """Одна учётка в двух компаниях: ушла из одной — данные во второй целы."""
    other = Tenant(id=uuid4(), company_code="other", name="Other Co")
    session.add(other)
    await session.commit()
    common: dict[str, dict] = {}
    for tenant in (tenant_ctx, other):
        with tenant_scope(tenant.id):
            department = Department(tenant_id=tenant.id, name="Склад")
            connector = Connector(
                tenant_id=tenant.id, kind="yandex360", name="Диск", mode="per_user"
            )
            material = Material(tenant_id=tenant.id, title="Документ", content="")
            session.add_all([department, connector, material])
            await session.commit()
        common[str(tenant.id)] = {
            "department": department,
            "connector": connector,
            "material": material,
        }
    left = await _member(
        session,
        tenant_ctx,
        "anna@test.com",
        left_days_ago=45,
        **common[str(tenant_ctx.id)],
    )
    with tenant_scope(other.id):
        stays = make_user(
            tenant_id=other.id,
            email="anna-2@test.com",
            account=left.account,
            position="Кладовщик",
        )
        session.add(stays)
        await session.commit()
    await _conversation(
        session,
        stays,
        created_at=NOW - timedelta(days=50),
        updated_at=NOW - timedelta(days=40),
    )

    await RetentionService(session_maker, qa_log_days=90).purge(now=NOW)

    assert (await _left_of(session, tenant_ctx.id, left.id))["conversations"] == 0
    kept = await _left_of(session, other.id, stays.id)
    assert kept["conversations"] == 1
    assert kept["profile"][0] == "Кладовщик"
