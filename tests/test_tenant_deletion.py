"""Удаление данных компании после расторжения (TenantDeletionService).

Удаляется всё компании, кроме финансовых записей (обезличенных) и
журнала действий (по своему сроку); соседняя компания не задета —
удаление идёт в её tenant_scope под RLS с явным tenant_id. Каждая
таблица с tenant_id учтена: удаляется, остаётся или отвязывается.
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.registry import default_registry
from corp_ed.core.config import EMBEDDING_DIM, ConnectorSettings
from corp_ed.core.database import Base
from corp_ed.core.exceptions import CodedConflictError
from corp_ed.core.secrets import SecretBox
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import (
    Account,
    AuditEvent,
    ChatSuggestion,
    Chunk,
    CompanyRequest,
    Connector,
    ConnectorSyncJob,
    ConnectorSyncRun,
    ConnectorUserGrant,
    CreditGrant,
    CreditOrder,
    CreditSpend,
    CreditTopupRequest,
    Department,
    Folder,
    FolderDepartment,
    GapCluster,
    GapClusterQuestion,
    GlossaryTerm,
    IngestJob,
    Invite,
    InviteLookup,
    Material,
    MaterialAccess,
    Notification,
    NotificationSetting,
    QaLog,
    RefreshToken,
    SupportRequest,
    Tenant,
    TenantLogo,
    UserRole,
)
from corp_ed.services.connector_service import TokenRevocation
from corp_ed.services.tenant_deletion_service import (
    DELETED_TABLES,
    GLOBAL_TABLES,
    KEPT_TABLES,
    TenantDeletionService,
)
from tests.connectors.fake_yandex import CLIENT_ID, CLIENT_SECRET, OAUTH_SERVER
from tests.factories import make_user
from tests.test_chat_retention import _conversation

KEY = Fernet.generate_key().decode()
NOW = datetime(2026, 10, 9, 3, 10, tzinfo=UTC)

UNLINKED = {"audit_events", "support_requests"}
"""Таблицы с tenant_id, которые не удаляются: журнал действий — по
сроку (правило базы), обращения в поддержку — данные учётки, ссылка на
компанию обнуляется."""


class Revocations:
    def __init__(self) -> None:
        self.calls: list[list[TokenRevocation]] = []

    async def __call__(self, revocations: Sequence[TokenRevocation]) -> None:
        self.calls.append(list(revocations))


def make_service(
    session_maker: async_sessionmaker[AsyncSession], revoke: Revocations
) -> TenantDeletionService:
    settings = ConnectorSettings(secrets_keys=KEY, yandex_oauth_server=OAUTH_SERVER)  # type: ignore[arg-type]
    return TenantDeletionService(
        session_maker,
        registry=default_registry(settings),
        secrets=SecretBox([KEY]),
        revoke=revoke,
        protected_codes=("demo-site",),
    )


async def _fill(session: AsyncSession, tenant: Tenant) -> dict[str, Any]:
    """По строке во всех таблицах компании и в связанных с ней."""
    box = SecretBox([KEY])
    code = tenant.company_code
    with tenant_scope(tenant.id):
        admin = make_user(
            tenant_id=tenant.id, email=f"admin@{code}.ru", role=UserRole.ADMIN
        )
        employee = make_user(tenant_id=tenant.id, email=f"anna@{code}.ru")
        department = Department(tenant_id=tenant.id, name="Склад")
        folder = Folder(tenant_id=tenant.id, name="Кадры", restricted=True)
        session.add_all([admin, employee, department, folder])
        await session.flush()
        employee.position = "Кладовщик"
        employee.department_id = department.id
        connector = Connector(
            tenant_id=tenant.id,
            kind="yandex360",
            name="Яндекс 360",
            mode="per_user",
            modules=["disk"],
            config={"client_id": CLIENT_ID},
            credentials=box.encrypt({"client_secret": CLIENT_SECRET}),
        )
        material = Material(
            tenant_id=tenant.id,
            title="Регламент",
            content="Отпуск — 28 дней.",
            visibility="restricted",
            folder_id=folder.id,
        )
        session.add_all([connector, material])
        await session.flush()
        qa = QaLog(
            tenant_id=tenant.id,
            user_id=employee.id,
            question="Сколько дней отпуска?",
            question_embedding=[0.1] * EMBEDDING_DIM,
            embedding_model="fake",
            prompt_version="test",
            answer_given=True,
            origin="documents",
        )
        cluster = GapCluster(
            tenant_id=tenant.id,
            title="Отпуск",
            priority=1.0,
            question_count=1,
            user_count=1,
            first_seen=NOW,
            last_seen=NOW,
            embedding_model="fake",
        )
        invite = Invite(
            tenant_id=tenant.id,
            token_hash=uuid4().hex,
            created_by=admin.id,
            expires_at=NOW + timedelta(days=7),
            max_uses=10,
        )
        order = CreditOrder(
            tenant_id=tenant.id,
            number=1,
            pack="s",
            credits=1000,
            amount_kopecks=99_000,
            status="paid",
            created_by=admin.id,
            paid_at=NOW,
        )
        session.add_all([qa, cluster, invite, order])
        await session.flush()
        grant = CreditGrant(
            tenant_id=tenant.id,
            credits=1000,
            remaining=900,
            source="purchase",
            order_id=order.id,
            comment="Оплатил Иванов",
            expires_at=NOW + timedelta(days=365),
        )
        session.add(grant)
        await session.flush()
        session.add_all(
            [
                FolderDepartment(
                    tenant_id=tenant.id,
                    folder_id=folder.id,
                    department_id=department.id,
                ),
                Chunk(
                    tenant_id=tenant.id,
                    material_id=material.id,
                    position=0,
                    content="Отпуск — 28 дней.",
                    embedding=[0.1] * EMBEDDING_DIM,
                    model="fake",
                    model_version="1",
                ),
                MaterialAccess(
                    tenant_id=tenant.id, material_id=material.id, user_id=employee.id
                ),
                ConnectorUserGrant(
                    tenant_id=tenant.id,
                    connector_id=connector.id,
                    user_id=employee.id,
                    credentials=box.encrypt(
                        {"access_token": f"y0-{code}", "refresh_token": "r"}
                    ),
                ),
                ConnectorSyncRun(
                    tenant_id=tenant.id, connector_id=connector.id, trigger="manual"
                ),
                GapClusterQuestion(
                    tenant_id=tenant.id, cluster_id=cluster.id, qa_log_id=qa.id
                ),
                GlossaryTerm(
                    tenant_id=tenant.id, term="ДМС", expansion="медицинское страхование"
                ),
                ChatSuggestion(tenant_id=tenant.id, text="Как оформить отпуск?"),
                Notification(
                    tenant_id=tenant.id,
                    user_id=admin.id,
                    kind="join_request",
                    title="Заявка: Анна",
                    body="",
                ),
                NotificationSetting(tenant_id=tenant.id, user_id=admin.id),
                CreditSpend(
                    tenant_id=tenant.id,
                    grant_id=grant.id,
                    credits=100,
                    period_start=NOW,
                ),
                CreditTopupRequest(
                    tenant_id=tenant.id, episode_start=NOW, requested_by=employee.id
                ),
            ]
        )
        await session.commit()
    await _conversation(session, employee, created_at=NOW, updated_at=NOW, shared=True)
    assert admin.account is not None and employee.account is not None
    admin.account.last_tenant_id = tenant.id
    session.add_all(
        [
            IngestJob(tenant_id=tenant.id, material_id=material.id),
            ConnectorSyncJob(tenant_id=tenant.id, connector_id=connector.id),
            InviteLookup(hash=uuid4().hex, tenant_id=tenant.id, invite_id=invite.id),
            RefreshToken(
                account_id=employee.account.id,
                user_id=employee.id,
                tenant_id=tenant.id,
                family_id=uuid4(),
                token_hash=uuid4().hex,
                expires_at=NOW + timedelta(days=30),
            ),
            TenantLogo(tenant_id=tenant.id, content=b"webp", version="1"),
            CompanyRequest(
                account_id=admin.account.id,
                company_name=tenant.name,
                status="approved",
                tenant_id=tenant.id,
            ),
            SupportRequest(
                account_id=employee.account.id,
                tenant_id=tenant.id,
                topic="other",
                message="Не вижу документ",
            ),
            AuditEvent(tenant_id=tenant.id, actor_user_id=admin.id, action="user.left"),
        ]
    )
    await session.commit()
    return {"admin": admin, "employee": employee, "order": order, "grant": grant}


def _company_tables() -> list[Any]:
    return [t for t in Base.metadata.sorted_tables if "tenant_id" in t.columns]


async def _counts(session: AsyncSession, tenant_id: UUID) -> dict[str, int]:
    """Сколько строк компании в каждой таблице с tenant_id — глазами
    самой компании (tenant_scope, RLS)."""
    session.expunge_all()
    result = {}
    with tenant_scope(tenant_id):
        for table in _company_tables():
            result[table.name] = int(
                await session.scalar(
                    select(func.count())
                    .select_from(table)
                    .where(table.c.tenant_id == tenant_id)
                )
                or 0
            )
        await session.commit()
    return result


def test_every_company_table_is_deleted_kept_or_unlinked() -> None:
    handled = (
        {m.__tablename__ for m in DELETED_TABLES}
        | {m.__tablename__ for m in KEPT_TABLES}
        | {m.__tablename__ for m in GLOBAL_TABLES}
        | UNLINKED
    )
    assert {t.name for t in _company_tables()} == handled


async def _suspended(session: AsyncSession, code: str) -> Tenant:
    tenant = Tenant(id=uuid4(), company_code=code, name=f"ИП {code}", is_active=False)
    tenant.email_domains = [f"{code}.ru"]
    tenant.pilot_until = date(2026, 12, 1)
    session.add(tenant)
    await session.commit()
    return tenant


async def test_deletes_everything_of_the_company_but_finance(
    session: AsyncSession, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    gone = await _suspended(session, "ivanov")
    neighbour = Tenant(id=uuid4(), company_code="petrov", name="ООО Петров")
    session.add(neighbour)
    await session.commit()
    rows = await _fill(session, gone)
    await _fill(session, neighbour)
    before = await _counts(session, neighbour.id)
    gone_before = await _counts(session, gone.id)
    assert all(gone_before[t.name] >= 1 for t in _company_tables())
    revoke = Revocations()

    report = await make_service(session_maker, revoke).delete(
        gone.id, " IVANOV ", now=NOW
    )

    after = await _counts(session, gone.id)
    kept = {m.__tablename__ for m in KEPT_TABLES}
    for table, count in after.items():
        if table in kept:
            assert count == gone_before[table], table
        elif table == "audit_events":
            # Журнал действий — по сроку; плюс запись об удалении.
            assert count == gone_before[table] + 1
        else:
            assert count == 0, table
    assert report.deleted["materials"] == 1
    assert report.deleted["users"] == 2
    assert report.deleted["conversations"] == 1
    # Соседняя компания не задета ни в одной таблице.
    assert await _counts(session, neighbour.id) == before

    # Строка компании обезличена, код освобождён.
    tenant = await session.get(Tenant, gone.id)
    assert tenant is not None
    assert tenant.name == f"Удалённая компания {str(gone.id)[:8]}"
    assert tenant.company_code == f"deleted-{gone.id.hex}"
    assert tenant.email_domains == []
    assert tenant.pilot_until is None
    assert tenant.is_active is False
    assert tenant.data_deleted_at == NOW

    # Финансы: суммы на месте, кто заказал и комментарий — нет.
    with tenant_scope(gone.id):
        order = (await session.scalars(select(CreditOrder))).one()
        grant = (await session.scalars(select(CreditGrant))).one()
        await session.commit()
    assert (order.amount_kopecks, order.created_by) == (99_000, None)
    assert (grant.remaining, grant.comment) == (900, None)

    # Учётки людей живы; ссылки на компанию сняты.
    admin_account = await session.get(Account, rows["admin"].account_id)
    assert admin_account is not None and admin_account.last_tenant_id is None
    support = (
        await session.scalars(
            select(SupportRequest).where(
                SupportRequest.account_id == rows["employee"].account_id
            )
        )
    ).one()
    assert support.tenant_id is None

    # Событие в журнале — без названия и кода компании.
    event = (
        await session.scalars(
            select(AuditEvent).where(AuditEvent.action == "tenant.data_deleted")
        )
    ).one()
    assert event.tenant_id == gone.id
    assert event.details["ref"] == str(gone.id)[:8]
    assert "ivanov" not in str(event.details).casefold()
    old = (
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.tenant_id == gone.id, AuditEvent.action == "user.left"
            )
        )
    ).one()
    assert old.actor_user_id is None

    # Токены сотрудников отзываются у провайдера — только этой компании.
    [revocations] = revoke.calls
    [revocation] = revocations
    assert [t["access_token"] for t in revocation.tokens] == ["y0-ivanov"]

    # Код свободен: новая компания может его занять.
    session.add(Tenant(id=uuid4(), company_code="ivanov", name="ИП Иванов"))
    await session.commit()


@pytest.mark.parametrize(
    ("active", "code", "error"),
    [
        (True, "ivanov", "tenant_active"),
        (False, "petrov", "code_mismatch"),
        (False, "", "code_mismatch"),
    ],
)
async def test_refuses_active_company_or_wrong_code(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    active: bool,
    code: str,
    error: str,
) -> None:
    tenant = await _suspended(session, "ivanov")
    tenant.is_active = active
    await session.commit()
    await _fill(session, tenant)
    before = await _counts(session, tenant.id)

    with pytest.raises(CodedConflictError) as caught:
        await make_service(session_maker, Revocations()).delete(tenant.id, code)

    assert caught.value.code == error
    assert await _counts(session, tenant.id) == before


async def test_refuses_twice_and_the_sandbox_company(
    session: AsyncSession, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _suspended(session, "ivanov")
    service = make_service(session_maker, Revocations())
    await service.delete(tenant.id, "ivanov")
    with pytest.raises(CodedConflictError) as caught:
        await service.delete(tenant.id, f"deleted-{tenant.id.hex}")
    assert caught.value.code == "tenant_data_deleted"

    demo = await _suspended(session, "demo-site")
    with pytest.raises(CodedConflictError) as caught:
        await service.delete(demo.id, "demo-site")
    assert caught.value.code == "tenant_protected"


async def test_cli_asks_for_the_code_and_prints_counts(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from corp_ed import cli

    tenant = await _suspended(session, "ivanov")
    await _fill(session, tenant)
    monkeypatch.setattr(cli, "get_session_maker", lambda: session_maker)
    # Ключи подключений у CLI не настроены: токены не отозвать, но
    # удалению это не мешает (отзыв проверен выше); наружу тест не ходит.
    typed: list[str] = []
    monkeypatch.setattr(
        "builtins.input", lambda prompt: typed.append(prompt) or "ivanov"
    )
    code = await cli._run(
        cli._parser().parse_args(["delete-tenant", "--code", "ivanov"])
    )

    assert code == 0
    assert typed == ["Введите код компании для подтверждения: "]
    out = capsys.readouterr().out
    assert "«ИП ivanov»" in out
    assert f"теперь она — {str(tenant.id)[:8]}" in out
    assert "  users: 2" in out
    await session.refresh(tenant)
    assert tenant.data_deleted_at is not None
