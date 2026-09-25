from typing import Annotated
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse, RedirectResponse, Response

from corp_ed.api.v1.dependencies import (
    get_connector_service,
    get_current_user,
    require_role,
)
from corp_ed.api.v1.rate_limits import (
    CONNECTOR_GRANT_PER_USER,
    CONNECTOR_OAUTH_CALLBACK_PER_IP,
    CONNECTOR_SYNC_PER_TENANT,
    CONNECTOR_TEST_PER_TENANT,
    CONNECTOR_WRITE_PER_TENANT,
    limit_by_ip,
    limit_by_tenant,
    limit_by_user,
)
from corp_ed.api.v1.schemas.connector import (
    ConnectorCreateRequest,
    ConnectorKindResponse,
    ConnectorResponse,
    ConnectorTestResponse,
    ConnectorUpdateRequest,
    CredentialsRequest,
    FieldSpecResponse,
    ModuleSpecResponse,
    MyConnectorResponse,
    OAuthCallbackResponse,
    OAuthStartResponse,
    SyncRequestedResponse,
    SyncRunResponse,
)
from corp_ed.connectors.registry import FieldSpec, KindSpec, UnknownKindError
from corp_ed.domain.models import User, UserRole
from corp_ed.domain.types import GrantStatus
from corp_ed.services.connector_service import ConnectorService

router = APIRouter(prefix="/connectors", tags=["connectors"])

AdminUser = Annotated[User, Depends(require_role(UserRole.ADMIN))]
AnyUser = Annotated[User, Depends(get_current_user)]
Service = Annotated[ConnectorService, Depends(get_connector_service)]


def _fields(specs: tuple[FieldSpec, ...]) -> list[FieldSpecResponse]:
    return [
        FieldSpecResponse(
            name=f.name, title=f.title, required=f.required, secret=f.secret
        )
        for f in specs
    ]


def _kind(spec: KindSpec, callback_url: str | None) -> ConnectorKindResponse:
    return ConnectorKindResponse(
        kind=spec.kind,
        title=spec.title,
        mode=spec.mode,
        modules=[ModuleSpecResponse(name=m.name, title=m.title) for m in spec.modules],
        config_fields=_fields(spec.config_fields),
        credential_fields=_fields(spec.credential_fields),
        app_credential_fields=_fields(spec.app_credential_fields),
        oauth=spec.oauth,
        oauth_callback_url=callback_url if spec.oauth else None,
        extra=dict(spec.extra),
    )


def _is_oauth(service: ConnectorService, kind: str) -> bool:
    try:
        return service.registry.spec(kind).oauth
    except UnknownKindError:
        return False


# --- каталог и список (ADMIN) ---------------------------------------------------


@router.get("/kinds", response_model=list[ConnectorKindResponse])
async def list_kinds(
    service: Service, current_user: AdminUser
) -> list[ConnectorKindResponse]:
    """Какие системы можно подключить и какие поля у формы."""
    callback_url = service.settings.oauth_callback_url
    return [_kind(spec, callback_url) for spec in service.kinds()]


@router.get("", response_model=list[ConnectorResponse])
async def list_connectors(
    service: Service, current_user: AdminUser
) -> list[ConnectorResponse]:
    return [ConnectorResponse.model_validate(c) for c in await service.list_all()]


@router.post(
    "",
    response_model=ConnectorResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(limit_by_tenant(CONNECTOR_WRITE_PER_TENANT))],
)
async def create_connector(
    data: ConnectorCreateRequest, service: Service, current_user: AdminUser
) -> ConnectorResponse:
    """Создать подключение. Учётные данные — отдельным PUT .../credentials
    (режим organization) или каждым сотрудником (режим per_user)."""
    connector = await service.create(
        current_user,
        kind=data.kind,
        name=data.name,
        modules=data.modules,
        config=data.config,
        sync_interval_minutes=data.sync_interval_minutes,
    )
    return ConnectorResponse.model_validate(connector)


# --- сотрудник: мои источники (до /{connector_id}, иначе «mine» — это id) ------


@router.get("/mine", response_model=list[MyConnectorResponse])
async def my_connectors(
    service: Service, current_user: AnyUser
) -> list[MyConnectorResponse]:
    """Подключения, которые сотрудник авторизует сам, и его состояние в них."""
    return [
        MyConnectorResponse(
            id=connector.id,
            kind=connector.kind,
            name=connector.name,
            grant_status=GrantStatus(grant.status) if grant else None,
            grant_error_code=grant.error_code if grant else None,
            oauth=_is_oauth(service, connector.kind),
        )
        for connector, grant in await service.my_connectors(current_user)
    ]


# --- OAuth режима per_user (до /{connector_id}: «oauth» — не id) ---------------


@router.get(
    "/oauth/callback",
    response_model=OAuthCallbackResponse,
    dependencies=[Depends(limit_by_ip(CONNECTOR_OAUTH_CALLBACK_PER_IP))],
)
async def oauth_callback(
    service: Service,
    state: Annotated[str, Query(min_length=1, max_length=2048)],
    code: Annotated[str | None, Query(max_length=256)] = None,
    error: Annotated[str | None, Query(max_length=128)] = None,
) -> Response:
    """Возврат браузера сотрудника с портала: код → токены → грант.

    Без аутентификации: кто и к какому подключению — из подписанного
    state. С CONNECTOR_OAUTH_RETURN_URL — редирект на фронт с
    connector_id и status (ok / error и error_code); без него — JSON.
    Ошибка — тоже 200 с кодом: браузеру некуда «упасть». Отказ в
    согласии приходит без code, с параметром error (OAuth 2.0).
    """
    result = await service.oauth_callback(state, code, provider_error=error)
    return_url = service.settings.oauth_return_url
    if return_url is None:
        return JSONResponse(
            OAuthCallbackResponse(
                ok=result.ok,
                connector_id=result.connector_id,
                error_code=result.error_code,
            ).model_dump(mode="json")
        )
    query = {"status": "ok" if result.ok else "error"}
    if result.connector_id is not None:
        query["connector_id"] = str(result.connector_id)
    if result.error_code is not None:
        query["error_code"] = result.error_code
    return RedirectResponse(
        _with_query(return_url, query), status_code=status.HTTP_303_SEE_OTHER
    )


@router.post(
    "/{connector_id}/oauth/start",
    response_model=OAuthStartResponse,
    dependencies=[Depends(limit_by_user(CONNECTOR_GRANT_PER_USER))],
)
async def oauth_start(
    connector_id: UUID, service: Service, current_user: AnyUser
) -> OAuthStartResponse:
    """Адрес авторизации на портале: фронт открывает его в браузере
    сотрудника, портал вернёт браузер на /connectors/oauth/callback."""
    return OAuthStartResponse(
        authorize_url=await service.oauth_start(current_user, connector_id)
    )


def _with_query(url: str, params: dict[str, str]) -> str:
    parts = urlsplit(url)
    query = f"{parts.query}&{urlencode(params)}" if parts.query else urlencode(params)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


@router.put(
    "/{connector_id}/mine",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(limit_by_user(CONNECTOR_GRANT_PER_USER))],
)
async def set_my_credentials(
    connector_id: UUID,
    data: CredentialsRequest,
    service: Service,
    current_user: AnyUser,
) -> None:
    """Авторизовать себя в источнике (режим per_user). Документы из
    листинга сотрудника появятся после ближайшей синхронизации."""
    await service.set_my_credentials(current_user, connector_id, data.credentials)


@router.delete(
    "/{connector_id}/mine",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(limit_by_user(CONNECTOR_GRANT_PER_USER))],
)
async def revoke_my_credentials(
    connector_id: UUID, service: Service, current_user: AnyUser
) -> None:
    await service.revoke_my_credentials(current_user, connector_id)


# --- одно подключение (ADMIN) ---------------------------------------------------


@router.get("/{connector_id}", response_model=ConnectorResponse)
async def get_connector(
    connector_id: UUID, service: Service, current_user: AdminUser
) -> ConnectorResponse:
    return ConnectorResponse.model_validate(await service.get(connector_id))


@router.patch(
    "/{connector_id}",
    response_model=ConnectorResponse,
    dependencies=[Depends(limit_by_tenant(CONNECTOR_WRITE_PER_TENANT))],
)
async def update_connector(
    connector_id: UUID,
    data: ConnectorUpdateRequest,
    service: Service,
    current_user: AdminUser,
) -> ConnectorResponse:
    connector = await service.update(
        current_user,
        connector_id,
        name=data.name,
        modules=data.modules,
        config=data.config,
        sync_interval_minutes=data.sync_interval_minutes,
        status=data.status,
    )
    return ConnectorResponse.model_validate(connector)


@router.delete(
    "/{connector_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(limit_by_tenant(CONNECTOR_WRITE_PER_TENANT))],
)
async def delete_connector(
    connector_id: UUID, service: Service, current_user: AdminUser
) -> None:
    """Удалить подключение вместе с его документами."""
    await service.delete(current_user, connector_id)


@router.put(
    "/{connector_id}/credentials",
    response_model=ConnectorResponse,
    dependencies=[Depends(limit_by_tenant(CONNECTOR_WRITE_PER_TENANT))],
)
async def set_credentials(
    connector_id: UUID,
    data: CredentialsRequest,
    service: Service,
    current_user: AdminUser,
) -> ConnectorResponse:
    """Учётные данные подключения (режим organization). Только запись:
    в ответах — лишь credentials_set_at. Ставит синхронизацию в очередь."""
    connector = await service.set_credentials(
        current_user, connector_id, data.credentials
    )
    return ConnectorResponse.model_validate(connector)


@router.post(
    "/{connector_id}/test",
    response_model=ConnectorTestResponse,
    dependencies=[Depends(limit_by_tenant(CONNECTOR_TEST_PER_TENANT))],
)
async def test_connector(
    connector_id: UUID, service: Service, current_user: AdminUser
) -> ConnectorTestResponse:
    """Проверить учётные данные без загрузки документов."""
    result = await service.check(current_user, connector_id)
    return ConnectorTestResponse(ok=result.ok, error_code=result.error_code)


@router.post(
    "/{connector_id}/sync",
    response_model=SyncRequestedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(limit_by_tenant(CONNECTOR_SYNC_PER_TENANT))],
)
async def sync_now(
    connector_id: UUID, service: Service, current_user: AdminUser
) -> SyncRequestedResponse:
    """«Синхронизировать сейчас»: 202, работа — в воркере; прогресс — в runs."""
    queued = await service.request_sync(current_user, connector_id)
    return SyncRequestedResponse(connector_id=connector_id, queued=queued)


@router.get("/{connector_id}/runs", response_model=list[SyncRunResponse])
async def list_runs(
    connector_id: UUID, service: Service, current_user: AdminUser
) -> list[SyncRunResponse]:
    return [
        SyncRunResponse.model_validate(r) for r in await service.runs_for(connector_id)
    ]
