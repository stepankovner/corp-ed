"""Коннекторы со стороны API: настроить, проверить, поставить в очередь.

Сама синхронизация — ConnectorSyncService в воркере. Здесь — проверки
формы (по спецификации вида), потолок на компанию, шифрование учётных
данных, аудит и постановка задач. Учётные данные принимаются и никогда
не возвращаются; в аудит и логи попадают только имена полей.
"""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import jwt
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.api.v1.schemas.connector import MAX_FIELD_VALUE_LENGTH
from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterError,
    SourceAdapter,
    refreshed_credentials,
)
from corp_ed.connectors.registry import (
    AdapterRegistry,
    FieldSpec,
    KindSpec,
    OAuthNotSupportedError,
    UnknownKindError,
)
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.exceptions import (
    ConnectorLimitError,
    ConnectorStateError,
    InvalidConnectorConfigError,
    NotFoundError,
)
from corp_ed.core.outbound import (
    OutboundClient,
    OutboundURLError,
    Resolver,
    validate_outbound_url,
)
from corp_ed.core.secrets import SecretBox, SecretDecryptionError
from corp_ed.core.security import create_oauth_state, decode_oauth_state
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import Connector, ConnectorSyncRun, ConnectorUserGrant, User
from corp_ed.domain.types import (
    ConnectorMode,
    ConnectorStatus,
    GrantStatus,
    SyncTrigger,
)
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.repositories.connector_repository import (
    ConnectorRepository,
    GrantRepository,
    SyncRunRepository,
)
from corp_ed.repositories.connector_sync_job_repository import (
    ConnectorSyncJobRepository,
)
from corp_ed.repositories.material_repository import MaterialRepository
from corp_ed.repositories.user_repository import UserRepository

logger = structlog.get_logger()

CHECK_TIMEOUT = 20.0
"""Проверка учётных данных — один запрос к системе клиента; дольше —
источник недоступен, а не «подумаем ещё»."""
RUNS_LIMIT = 50


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    error_code: str | None = None


@dataclass(frozen=True)
class OAuthResult:
    """Итог обратного вызова: ошибка — код для страницы фронта, не исключение:
    браузер сотрудника надо вернуть на фронт в любом случае."""

    connector_id: UUID | None
    error_code: str | None = None

    @property
    def ok(self) -> bool:
        return self.error_code is None


class ConnectorService:
    def __init__(
        self,
        connectors: ConnectorRepository,
        grants: GrantRepository,
        runs: SyncRunRepository,
        jobs: ConnectorSyncJobRepository,
        materials: MaterialRepository,
        audit: AuditRepository,
        secrets: SecretBox,
        registry: AdapterRegistry,
        settings: ConnectorSettings,
        session: AsyncSession,
        http: OutboundClient,
        *,
        resolver: Resolver | None = None,
    ) -> None:
        self.connectors = connectors
        self.grants = grants
        self.runs = runs
        self.jobs = jobs
        self.materials = materials
        self.audit = audit
        self.secrets = secrets
        self.registry = registry
        self.settings = settings
        self.session = session
        self.http = http
        self.resolver = resolver

    # --- каталог и чтение -----------------------------------------------------

    def kinds(self) -> list[KindSpec]:
        return self.registry.kinds()

    async def list_all(self) -> list[Connector]:
        return await self.connectors.list_all()

    async def get(self, connector_id: UUID) -> Connector:
        connector = await self.connectors.get_by_id(connector_id)
        if connector is None:
            raise NotFoundError("Подключение не найдено")
        return connector

    async def runs_for(self, connector_id: UUID) -> list[ConnectorSyncRun]:
        connector = await self.get(connector_id)
        return await self.runs.list_for_connector(connector.id, limit=RUNS_LIMIT)

    # --- настройка (ADMIN) ----------------------------------------------------

    async def create(
        self,
        actor: User,
        *,
        kind: str,
        name: str,
        modules: list[str],
        config: Mapping[str, str],
        sync_interval_minutes: int | None,
    ) -> Connector:
        spec = self._spec(kind)
        if await self.connectors.count() >= self.settings.max_per_tenant:
            raise ConnectorLimitError(self.settings.max_per_tenant)
        clean_modules = _validate_modules(spec, modules)
        clean_config = await _validate_config(spec, config, self.resolver)
        connector = await self.connectors.create(
            Connector(
                kind=spec.kind,
                name=name.strip(),
                mode=spec.mode.value,
                modules=clean_modules,
                config=clean_config,
                sync_interval_minutes=(
                    sync_interval_minutes or self.settings.default_sync_interval_minutes
                ),
                created_by=actor.id,
            )
        )
        self._record(
            AuditAction.CONNECTOR_CREATED,
            actor,
            connector,
            {"kind": spec.kind, "modules": clean_modules},
        )
        await self.session.commit()
        logger.info("connector_created", connector_id=str(connector.id), kind=kind)
        return connector

    async def update(
        self,
        actor: User,
        connector_id: UUID,
        *,
        name: str | None,
        modules: list[str] | None,
        config: Mapping[str, str] | None,
        sync_interval_minutes: int | None,
        status: ConnectorStatus | None,
    ) -> Connector:
        connector = await self.get(connector_id)
        spec = self._spec(connector.kind)
        changed: dict[str, object] = {}
        if name is not None and name.strip() != connector.name:
            connector.name = name.strip()
            changed["name"] = connector.name
        if modules is not None:
            connector.modules = _validate_modules(spec, modules)
            changed["modules"] = connector.modules
        if config is not None:
            connector.config = await _validate_config(spec, config, self.resolver)
            changed["config"] = sorted(connector.config)
        if sync_interval_minutes is not None:
            connector.sync_interval_minutes = sync_interval_minutes
            changed["sync_interval_minutes"] = sync_interval_minutes
        if status is not None and status.value != connector.status:
            if status is ConnectorStatus.ERROR:
                raise InvalidConnectorConfigError(
                    "status_not_allowed", "Статус error выставляет только система"
                )
            if connector.status == ConnectorStatus.ERROR.value:
                raise ConnectorStateError(
                    "Подключение остановлено из-за учётных данных: "
                    "задайте новые, статус снимется сам"
                )
            connector.status = status.value
            changed["status"] = status.value
        if changed:
            self._record(AuditAction.CONNECTOR_UPDATED, actor, connector, changed)
        await self.session.commit()
        await self.session.refresh(connector)
        return connector

    async def delete(self, actor: User, connector_id: UUID) -> None:
        """Удалить подключение вместе с его документами, грантами и
        журналом: ответы по документам источника прекращаются сразу."""
        connector = await self.get(connector_id)
        self._record(
            AuditAction.CONNECTOR_DELETED,
            actor,
            connector,
            {"kind": connector.kind, "name": connector.name},
        )
        await self.connectors.delete(connector)
        await self.session.commit()
        logger.info("connector_deleted", connector_id=str(connector_id))

    async def set_credentials(
        self, actor: User, connector_id: UUID, credentials: Mapping[str, str]
    ) -> Connector:
        """Учётные данные подключения (режим organization). Только запись.

        Новые данные снимают остановку по ошибке и ставят синхронизацию
        в очередь: админ сразу видит, заработало ли.
        """
        connector = await self.get(connector_id)
        spec = self._spec(connector.kind)
        if connector.mode == ConnectorMode.ORGANIZATION.value:
            fields = spec.credential_fields
        elif spec.app_credential_fields:
            # Режим per_user с приложением: секрет приложения — админа,
            # токены — каждого сотрудника (грант).
            fields = spec.app_credential_fields
        else:
            raise ConnectorStateError(
                "В этом подключении каждый сотрудник авторизуется сам"
            )
        clean = _validate_fields(fields, credentials, "credentials")
        connector.credentials = self.secrets.encrypt(clean)
        connector.credentials_set_at = _now()
        if connector.status == ConnectorStatus.ERROR.value:
            connector.status = ConnectorStatus.ACTIVE.value
            connector.last_error_code = None
        self._record(
            AuditAction.CONNECTOR_CREDENTIALS_SET,
            actor,
            connector,
            {"fields": sorted(clean)},
        )
        await self.jobs.enqueue(connector.tenant_id, connector.id, SyncTrigger.MANUAL)
        await self.session.commit()
        await self.session.refresh(connector)
        return connector

    async def check(self, actor: User, connector_id: UUID) -> CheckResult:
        """Проверить учётные данные без загрузки документов.

        В режиме organization — данные подключения; в per_user — грант
        того, кто проверяет. Результат — код, не текст источника.
        """
        connector = await self.connectors.get_with_credentials(connector_id)
        if connector is None:
            raise NotFoundError("Подключение не найдено")
        # Вид без адаптера в этой сборке — 422 с кодом, а не 500.
        spec = self._spec(connector.kind)
        token: str | None
        grant: ConnectorUserGrant | None = None
        app_credentials: Mapping[str, str] = {}
        if connector.mode == ConnectorMode.ORGANIZATION.value:
            token = connector.credentials
        else:
            grant = await self._grant_with_credentials(connector.id, actor.id)
            token = grant.credentials if grant is not None else None
            if spec.app_credential_fields:
                if connector.credentials is None:
                    return CheckResult(False, "app_credentials_missing")
                try:
                    app_credentials = self.secrets.decrypt(connector.credentials)
                except SecretDecryptionError:
                    return CheckResult(False, "credentials_unreadable")
        if token is None:
            return CheckResult(False, "credentials_missing")
        try:
            credentials = self.secrets.decrypt(token)
        except SecretDecryptionError:
            return CheckResult(False, "credentials_unreadable")
        config = {str(k): str(v) for k, v in connector.config.items()}
        adapter: SourceAdapter | None = None
        try:
            adapter = self.registry.build(
                connector.kind, config, {**app_credentials, **credentials}, self.http
            )
            async with asyncio.timeout(CHECK_TIMEOUT):
                await adapter.check()
        except AdapterAuthError as exc:
            return CheckResult(False, exc.code)
        except (AdapterError, OutboundURLError) as exc:
            return CheckResult(False, getattr(exc, "code", "source_unavailable"))
        except TimeoutError:
            return CheckResult(False, "timeout")
        finally:
            # Проверка могла продлить токены: новый refresh — единственный.
            if grant is not None and adapter is not None:
                await self._persist_refresh(grant, adapter)
        return CheckResult(True)

    # --- OAuth (режим per_user) ---------------------------------------------------

    async def oauth_start(self, user: User, connector_id: UUID) -> str:
        """Адрес авторизации на портале для этого сотрудника и подключения.

        state подписан и привязан к сотруднику, компании и подключению:
        обратный вызов придёт без нашего токена.
        """
        connector = await self.connectors.get_with_credentials(connector_id)
        if connector is None:
            raise NotFoundError("Подключение не найдено")
        spec = self._spec(connector.kind)
        if connector.mode != ConnectorMode.PER_USER.value or not spec.oauth:
            raise ConnectorStateError("Это подключение не использует OAuth")
        if connector.credentials is None:
            raise ConnectorStateError(
                "Администратор ещё не задал секрет приложения для этого подключения"
            )
        try:
            app_credentials = self.secrets.decrypt(connector.credentials)
        except SecretDecryptionError as exc:
            raise ConnectorStateError(
                "Секрет приложения не читается: обратитесь к администратору"
            ) from exc
        config = {str(k): str(v) for k, v in connector.config.items()}
        try:
            flow = self.registry.build_oauth(
                connector.kind, config, app_credentials, self.http
            )
        except OAuthNotSupportedError as exc:
            raise ConnectorStateError("Это подключение не использует OAuth") from exc
        except AdapterError as exc:
            raise InvalidConnectorConfigError(
                exc.code, "Приложение настроено не полностью"
            ) from exc
        state = create_oauth_state(
            user.id,
            connector.tenant_id,
            connector.id,
            ttl_minutes=self.settings.oauth_state_ttl_minutes,
        )
        return flow.authorize_url(state)

    async def oauth_callback(self, state: str, code: str) -> OAuthResult:
        """Обменять код на токены сотрудника и записать грант.

        Вызывается без аутентификации: кто и куда — только из state.
        Любая ошибка — код в результате и событие аудита, не исключение.
        """
        try:
            payload = decode_oauth_state(state)
            user_id = UUID(str(payload["sub"]))
            tenant_id = UUID(str(payload["tenant_id"]))
            connector_id = UUID(str(payload["connector_id"]))
        except (jwt.PyJWTError, KeyError, ValueError, TypeError):
            logger.warning("connector_oauth_state_invalid")
            return OAuthResult(None, "state_invalid")
        with tenant_scope(tenant_id):
            return await self._oauth_exchange(user_id, connector_id, code)

    async def _oauth_exchange(
        self, user_id: UUID, connector_id: UUID, code: str
    ) -> OAuthResult:
        connector = await self.connectors.get_with_credentials(connector_id)
        if connector is None:
            return OAuthResult(None, "connector_not_found")
        # Плоские значения: после rollback в _oauth_failed атрибуты
        # ORM-объектов истекают, и чтение ушло бы в базу из синхронного кода.
        tenant_id = connector.tenant_id
        user = await UserRepository(self.session).get_by_id(user_id)
        if user is None or not user.is_active or user.tenant_id != tenant_id:
            return await self._oauth_failed(
                tenant_id, connector_id, None, "user_not_found"
            )
        try:
            spec = self.registry.spec(connector.kind)
        except UnknownKindError:
            return await self._oauth_failed(
                tenant_id, connector_id, user_id, "kind_unknown"
            )
        if not spec.oauth or connector.credentials is None:
            return await self._oauth_failed(
                tenant_id, connector_id, user_id, "app_credentials_missing"
            )
        try:
            app_credentials = self.secrets.decrypt(connector.credentials)
        except SecretDecryptionError:
            return await self._oauth_failed(
                tenant_id, connector_id, user_id, "credentials_unreadable"
            )
        config = {str(k): str(v) for k, v in connector.config.items()}
        external_user_id: str | None = None
        try:
            flow = self.registry.build_oauth(
                connector.kind, config, app_credentials, self.http
            )
            async with asyncio.timeout(CHECK_TIMEOUT):
                exchanged = await flow.exchange(code)
                # Токен сразу проверяется на портале: сотрудник узнаёт о
                # проблеме здесь, а не через час из журнала синхронизации.
                adapter = self.registry.build(
                    connector.kind,
                    config,
                    {**app_credentials, **exchanged.credentials},
                    self.http,
                )
                await adapter.check()
            external_user_id = exchanged.external_user_id or getattr(
                adapter, "external_user_id", None
            )
            credentials = refreshed_credentials(adapter) or exchanged.credentials
        except (AdapterError, OutboundURLError) as exc:
            return await self._oauth_failed(
                tenant_id,
                connector_id,
                user_id,
                getattr(exc, "code", "source_unavailable"),
            )
        except TimeoutError:
            return await self._oauth_failed(tenant_id, connector_id, user_id, "timeout")

        token = self.secrets.encrypt(credentials)
        grant = await self.grants.get(connector.id, user.id)
        if grant is None:
            grant = await self.grants.add(
                ConnectorUserGrant(
                    connector_id=connector.id,
                    user_id=user.id,
                    credentials=token,
                    external_user_id=external_user_id,
                )
            )
        else:
            grant.credentials = token
            grant.external_user_id = external_user_id
            grant.status = GrantStatus.ACTIVE.value
            grant.error_code = None
        self._record(
            AuditAction.CONNECTOR_GRANT_SET,
            user,
            connector,
            {"via": "oauth", "fields": sorted(credentials)},
        )
        if connector.status == ConnectorStatus.ACTIVE.value:
            await self.jobs.enqueue(
                connector.tenant_id, connector.id, SyncTrigger.MANUAL
            )
        await self.session.commit()
        logger.info(
            "connector_oauth_granted",
            connector_id=str(connector.id),
            user_id=str(user.id),
        )
        return OAuthResult(connector.id)

    async def _oauth_failed(
        self, tenant_id: UUID, connector_id: UUID, user_id: UUID | None, code: str
    ) -> OAuthResult:
        await self.session.rollback()
        self.audit.record(
            AuditAction.CONNECTOR_OAUTH_FAILED,
            tenant_id=tenant_id,
            actor_id=user_id,
            target_type="connector",
            target_id=connector_id,
            details={"code": code},
        )
        await self.session.commit()
        logger.warning(
            "connector_oauth_failed", connector_id=str(connector_id), code=code
        )
        return OAuthResult(connector_id, code)

    async def _persist_refresh(
        self, grant: ConnectorUserGrant, adapter: SourceAdapter
    ) -> None:
        refreshed = refreshed_credentials(adapter)
        if refreshed is None:
            return
        grant.credentials = self.secrets.encrypt(refreshed)
        await self.session.commit()

    async def request_sync(self, actor: User, connector_id: UUID) -> bool:
        """«Синхронизировать сейчас». False — задача уже в очереди."""
        connector = await self.get(connector_id)
        if connector.status != ConnectorStatus.ACTIVE.value:
            raise ConnectorStateError("Подключение приостановлено или остановлено")
        queued = await self.jobs.enqueue(
            connector.tenant_id, connector.id, SyncTrigger.MANUAL
        )
        self._record(AuditAction.CONNECTOR_SYNC_REQUESTED, actor, connector)
        await self.session.commit()
        return queued

    # --- сотрудник: «подключить мои источники» --------------------------------

    async def my_connectors(
        self, user: User
    ) -> list[tuple[Connector, ConnectorUserGrant | None]]:
        grants = {g.connector_id: g for g in await self.grants.list_for_user(user.id)}
        return [
            (connector, grants.get(connector.id))
            for connector in await self.connectors.list_all()
            if connector.mode == ConnectorMode.PER_USER.value
        ]

    async def set_my_credentials(
        self, user: User, connector_id: UUID, credentials: Mapping[str, str]
    ) -> ConnectorUserGrant:
        connector = await self.get(connector_id)
        spec = self._spec(connector.kind)
        if connector.mode != ConnectorMode.PER_USER.value:
            raise ConnectorStateError("Это подключение настраивает администратор")
        if spec.oauth:
            raise InvalidConnectorConfigError(
                "oauth_required",
                "Этот источник подключается авторизацией на портале: "
                "POST /connectors/{id}/oauth/start",
            )
        clean = _validate_fields(spec.credential_fields, credentials, "credentials")
        token = self.secrets.encrypt(clean)
        grant = await self.grants.get(connector.id, user.id)
        if grant is None:
            grant = await self.grants.add(
                ConnectorUserGrant(
                    connector_id=connector.id, user_id=user.id, credentials=token
                )
            )
        else:
            grant.credentials = token
            grant.status = GrantStatus.ACTIVE.value
            grant.error_code = None
        self._record(
            AuditAction.CONNECTOR_GRANT_SET, user, connector, {"fields": sorted(clean)}
        )
        if connector.status == ConnectorStatus.ACTIVE.value:
            await self.jobs.enqueue(
                connector.tenant_id, connector.id, SyncTrigger.MANUAL
            )
        await self.session.commit()
        return grant

    async def revoke_my_credentials(self, user: User, connector_id: UUID) -> None:
        connector = await self.get(connector_id)
        grant = await self.grants.get(connector.id, user.id)
        if grant is None:
            raise NotFoundError("Вы не подключали этот источник")
        await self.grants.delete(grant)
        # Права были выведены из его листинга — без гранта их нет.
        await self.materials.revoke_all_access(connector.id, user.id)
        self._record(AuditAction.CONNECTOR_GRANT_REVOKED, user, connector)
        await self.session.commit()

    # --- вспомогательное ------------------------------------------------------

    def _spec(self, kind: str) -> KindSpec:
        try:
            return self.registry.spec(kind)
        except UnknownKindError as exc:
            raise InvalidConnectorConfigError(
                "kind_unknown", f"Неизвестный вид подключения: {kind}"
            ) from exc

    async def _grant_with_credentials(
        self, connector_id: UUID, user_id: UUID
    ) -> ConnectorUserGrant | None:
        for grant in await self.grants.list_active_with_credentials(connector_id):
            if grant.user_id == user_id:
                return grant
        return None

    def _record(
        self,
        action: AuditAction,
        actor: User,
        connector: Connector,
        details: Mapping[str, object] | None = None,
    ) -> None:
        self.audit.record(
            action,
            tenant_id=connector.tenant_id,
            actor_id=actor.id,
            target_type="connector",
            target_id=connector.id,
            details=dict(details or {}),
        )


def _validate_modules(spec: KindSpec, modules: list[str]) -> list[str]:
    unknown = sorted(set(modules) - spec.module_names)
    if unknown:
        raise InvalidConnectorConfigError(
            "module_unknown", f"Неизвестные модули: {', '.join(unknown)}"
        )
    ordered = [module.name for module in spec.modules if module.name in set(modules)]
    if not ordered:
        raise InvalidConnectorConfigError(
            "module_required", "Выберите хотя бы один модуль"
        )
    return ordered


def _validate_fields(
    fields: Sequence[FieldSpec], values: Mapping[str, str], what: str
) -> dict[str, str]:
    """Значения формы против спецификации: без лишнего и без пропусков."""
    known = {f.name: f for f in fields}
    unknown = sorted(set(values) - set(known))
    if unknown:
        raise InvalidConnectorConfigError(
            "field_unknown", f"Неизвестные поля {what}: {', '.join(unknown)}"
        )
    clean: dict[str, str] = {}
    for name, field in known.items():
        value = values.get(name)
        if value is None or not str(value).strip():
            if field.required:
                raise InvalidConnectorConfigError(
                    "field_required", f"Не заполнено поле {what}: {name}"
                )
            continue
        if not isinstance(value, str) or len(value) > MAX_FIELD_VALUE_LENGTH:
            raise InvalidConnectorConfigError(
                "field_invalid", f"Недопустимое значение поля {name}"
            )
        clean[name] = value.strip()
    return clean


async def _validate_config(
    spec: KindSpec, config: Mapping[str, str], resolver: Resolver | None
) -> dict[str, str]:
    clean = _validate_fields(spec.config_fields, config, "config")
    if spec.url_field and spec.url_field in clean:
        try:
            target = await validate_outbound_url(
                clean[spec.url_field], resolver=resolver
            )
        except OutboundURLError as exc:
            raise InvalidConnectorConfigError(
                exc.code, "Адрес системы не принят: только https и публичный адрес"
            ) from exc
        clean[spec.url_field] = target.url
    return clean


def _now() -> datetime:
    return datetime.now(UTC)
