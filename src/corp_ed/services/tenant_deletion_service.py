"""Удаление данных компании после расторжения (оферта п. 13.3).

Команда kronto запускает его в нашей панели («Удалить данные компании»)
или `cli delete-tenant`: только у приостановленной компании и только
после ввода её кода. Удаляется всё, что компания у нас оставила:
документы и их фрагменты, подключения с учётными данными и токенами
сотрудников (токены ещё и отзываются у провайдера), диалоги и вложения,
журнал вопросов и отчёт о пробелах, запуски синхронизации, папки,
отделы, глоссарий, подсказки, приглашения, уведомления, сотрудники
компании (членства), логотип, сеансы в этой компании, заявка на
подключение.

Остаётся:
- заказы пакетов кредитов, начисления и списания — бухгалтерский учёт
  (5 лет, ст. 29 402-ФЗ); кто заказал — обнуляется вместе с членствами,
  комментарий начисления стирается;
- строка компании — обезличенная («Удалённая компания 1a2b3c4d»): на
  неё ссылаются заказы;
- журнал действий — по своему сроку (365 дней): его записи нельзя
  удалить раньше по правилу базы (db_policies), ссылки на сотрудников в
  нём обнуляются;
- учётки людей — они не компании: человек сам решает, удалять ли её.

Каждая таблица компании (с tenant_id) должна быть в DELETED_TABLES или
KEPT_TABLES — это проверяет тест: новая таблица не останется забытой.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.registry import AdapterRegistry
from corp_ed.core.exceptions import CodedConflictError, NotFoundError
from corp_ed.core.secrets import SecretBox, SecretsNotConfiguredError
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.company_ref import company_ref
from corp_ed.domain.models import (
    Account,
    ChatAttachment,
    ChatAttachmentChunk,
    ChatMessage,
    ChatSuggestion,
    Chunk,
    CompanyRequest,
    Connector,
    ConnectorSyncJob,
    ConnectorSyncRun,
    ConnectorUserGrant,
    Conversation,
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
    User,
)
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.services.connector_service import (
    TokenRevocation,
    collect_revocation,
)

logger = structlog.get_logger()

DELETED_TABLES: tuple[type[Any], ...] = (
    # Порядок — по внешним ключам: сначала то, что ссылается.
    ChatMessage,
    ChatAttachmentChunk,
    ChatAttachment,
    Conversation,
    ChatSuggestion,
    Notification,
    NotificationSetting,
    GapClusterQuestion,
    GapCluster,
    QaLog,
    MaterialAccess,
    Chunk,
    Material,
    ConnectorUserGrant,
    ConnectorSyncRun,
    Connector,
    FolderDepartment,
    Folder,
    Department,
    GlossaryTerm,
    Invite,
    CreditTopupRequest,
    User,
)
"""Таблицы компании под RLS, которые удаляются целиком."""

KEPT_TABLES: tuple[type[Any], ...] = (CreditOrder, CreditGrant, CreditSpend)
"""Финансовые записи: нужны бухгалтерии, остаются обезличенными."""

GLOBAL_TABLES: tuple[type[Any], ...] = (
    IngestJob,
    ConnectorSyncJob,
    InviteLookup,
    RefreshToken,
    TenantLogo,
    CompanyRequest,
)
"""Таблицы вне RLS со ссылкой на компанию: удаляются по tenant_id."""

Revoker = Callable[[Sequence[TokenRevocation]], Awaitable[None]]


@dataclass(frozen=True)
class DeletionReport:
    tenant_id: UUID
    ref: str
    deleted: dict[str, int]
    """Таблица → сколько строк удалено."""


class TenantDeletionService:
    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        *,
        registry: AdapterRegistry,
        secrets: SecretBox,
        revoke: Revoker,
        protected_codes: Sequence[str] = (),
    ) -> None:
        self.session_maker = session_maker
        self.registry = registry
        self.secrets = secrets
        self.revoke = revoke
        # Песочница сайта: её компанию заводит и обновляет cli demo.
        self.protected_codes = frozenset(protected_codes)

    async def delete(
        self, tenant_id: UUID, confirm_code: str, now: datetime | None = None
    ) -> DeletionReport:
        """Удалить данные компании tenant_id.

        confirm_code — код компании, введённый человеком: защита от
        удаления не той компании. Компания должна быть приостановлена:
        удалить работающую нельзя ни случайно, ни намеренно в обход
        приостановки (её видят администраторы клиента и наша панель).
        """
        now = now or datetime.now(UTC)
        ref = company_ref(tenant_id)
        with tenant_scope(tenant_id):
            async with self.session_maker() as session:
                tenant = await session.scalar(
                    select(Tenant).where(Tenant.id == tenant_id).with_for_update()
                )
                if tenant is None:
                    raise NotFoundError("Компания не найдена")
                _check(tenant, confirm_code, self.protected_codes)
                revocations = await self._revocations(session, tenant_id)
                deleted = await _delete_all(session, tenant_id)
                await _anonymize(session, tenant, now)
                AuditRepository(session).record(
                    AuditAction.TENANT_DATA_DELETED,
                    tenant_id=tenant_id,
                    target_type="tenant",
                    target_id=tenant_id,
                    details={"ref": ref, "deleted": deleted},
                )
                await session.commit()
        logger.info("tenant_data_deleted", tenant_id=str(tenant_id), **deleted)
        # Токены сотрудников у провайдера — после коммита и best-effort:
        # данные у нас уже удалены, сбой провайдера этого не отменит.
        await self.revoke(revocations)
        return DeletionReport(tenant_id=tenant_id, ref=ref, deleted=deleted)

    async def _revocations(
        self, session: AsyncSession, tenant_id: UUID
    ) -> list[TokenRevocation]:
        connectors = (
            await session.scalars(
                select(Connector).where(Connector.tenant_id == tenant_id)
            )
        ).all()
        result: list[TokenRevocation] = []
        for connector in connectors:
            # credentials отложенная: подгрузить явно.
            await session.refresh(connector, ["credentials"])
            tokens = (
                await session.scalars(
                    select(ConnectorUserGrant.credentials).where(
                        ConnectorUserGrant.tenant_id == tenant_id,
                        ConnectorUserGrant.connector_id == connector.id,
                    )
                )
            ).all()
            try:
                revocation = collect_revocation(
                    connector,
                    [str(t) for t in tokens if t is not None],
                    self.registry,
                    self.secrets,
                )
            except SecretsNotConfiguredError:
                # Без ключей токены не прочитать и не отозвать — но
                # удалить их это не мешает.
                logger.warning(
                    "connector_token_revoke_failed",
                    connector_id=str(connector.id),
                    kind=connector.kind,
                    error="SecretsNotConfiguredError",
                    code="secrets_not_configured",
                )
                continue
            if revocation is not None:
                result.append(revocation)
        return result


def _check(tenant: Tenant, confirm_code: str, protected: frozenset[str]) -> None:
    if tenant.data_deleted_at is not None:
        raise CodedConflictError(
            "Данные этой компании уже удалены", "tenant_data_deleted"
        )
    if tenant.company_code in protected:
        raise CodedConflictError(
            "Это компания песочницы сайта — её данные обновляет cli demo",
            "tenant_protected",
        )
    if tenant.is_active:
        raise CodedConflictError(
            "Сначала приостановите компанию — удалить данные работающей нельзя",
            "tenant_active",
        )
    if confirm_code.strip().casefold() != tenant.company_code.casefold():
        raise CodedConflictError(
            "Код компании не совпадает — данные не удалены", "code_mismatch"
        )


async def _delete_all(session: AsyncSession, tenant_id: UUID) -> dict[str, int]:
    """Удалить строки компании; вернуть, сколько где удалено.

    Каждый запрос — с явным tenant_id, хотя сессия и так в tenant_scope:
    таблицы вне RLS он один и ограничивает, а для таблиц под RLS это
    второй рубеж, как во всех массовых удалениях.
    """
    deleted: dict[str, int] = {}
    for model in DELETED_TABLES:
        deleted[model.__tablename__] = await _delete(session, model, tenant_id)
    for model in GLOBAL_TABLES:
        deleted[model.__tablename__] = await _delete(session, model, tenant_id)
    # Учётки людей остаются; «последняя компания» больше не ведёт сюда.
    await session.execute(
        update(Account)
        .where(Account.last_tenant_id == tenant_id)
        .values(last_tenant_id=None)
        .execution_options(synchronize_session=False)
    )
    # Обращения в поддержку — от учётки, а не компании: остаются у
    # человека, но без ссылки на компанию.
    await session.execute(
        update(SupportRequest)
        .where(SupportRequest.tenant_id == tenant_id)
        .values(tenant_id=None)
        .execution_options(synchronize_session=False)
    )
    # Комментарий к начислению пишет команда свободным текстом — в нём
    # могут быть имена; сумма и сроки остаются.
    await session.execute(
        update(CreditGrant)
        .where(CreditGrant.tenant_id == tenant_id)
        .values(comment=None)
        .execution_options(synchronize_session=False)
    )
    return deleted


async def _delete(session: AsyncSession, model: type[Any], tenant_id: UUID) -> int:
    result = await session.execute(
        delete(model)
        .where(model.tenant_id == tenant_id)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def _anonymize(session: AsyncSession, tenant: Tenant, now: datetime) -> None:
    """Строка компании без названия и кода: код — транслитерация
    названия, у ИП это фамилия. Код освобождается — его может занять
    новая компания."""
    ref = company_ref(tenant.id)
    tenant.name = f"Удалённая компания {ref}"
    tenant.company_code = f"deleted-{tenant.id.hex}"
    tenant.email_domains = []
    tenant.pilot_until = None
    tenant.is_active = False
    tenant.data_deleted_at = now
    await session.flush()
