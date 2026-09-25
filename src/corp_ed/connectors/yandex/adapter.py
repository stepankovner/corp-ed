"""Спецификация вида yandex360 и адаптер (режим per_user, OAuth Яндекс ID).

Админ регистрирует приложение на oauth.yandex.ru (права
cloud_api:disk.read и cloud_api:disk.info, адрес возврата — наша ручка
обратного вызова), вводит client_id в config и client_secret в учётные
данные подключения; каждый сотрудник авторизует приложение сам.
Модуль disk — весь Диск сотрудника вместе с общими папками
Яндекс 360, которые в него смонтированы. Вики — следующим шагом.
"""

from collections.abc import AsyncIterator, Mapping, Sequence

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterConfigError,
    AdapterError,
    AdapterOptions,
    FetchedContent,
    RemoteDocument,
)
from corp_ed.connectors.common import (
    OAUTH_CALLBACK_PATH,
    Recorder,
    TokenSet,
    to_int,
)
from corp_ed.connectors.registry import (
    AdapterRegistry,
    FieldSpec,
    KindSpec,
    ModuleSpec,
    UserAuth,
)
from corp_ed.connectors.yandex import disk as disk_module
from corp_ed.connectors.yandex.disk import OAuthTokens, YandexDiskClient
from corp_ed.connectors.yandex.oauth import YandexOAuth
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.outbound import OutboundClient
from corp_ed.domain.types import ConnectorMode

KIND = "yandex360"
SPEC = KindSpec(
    kind=KIND,
    title="Яндекс 360 (Диск)",
    mode=ConnectorMode.PER_USER,
    user_auth=UserAuth.OAUTH,
    modules=(
        ModuleSpec(disk_module.MODULE_DISK, "Яндекс Диск сотрудника и общие папки"),
    ),
    config_fields=(FieldSpec("client_id", "ClientID приложения Яндекс ID"),),
    app_credential_fields=(
        FieldSpec("client_secret", "Client secret приложения Яндекс ID", secret=True),
    ),
    credential_fields=(),
    url_field=None,
    extra={
        "app_scopes": "cloud_api:disk.read,cloud_api:disk.info",
        "oauth_callback_path": OAUTH_CALLBACK_PATH,
    },
)


class YandexAdapter:
    def __init__(self, client: YandexDiskClient, *, max_bytes: int) -> None:
        self._client = client
        self._max_bytes = max_bytes
        self._user_id: str | None = None

    @property
    def refreshed_credentials(self) -> Mapping[str, str] | None:
        return self._client.refreshed_credentials

    @property
    def external_user_id(self) -> str | None:
        return self._user_id

    async def check(self) -> None:
        info = await self._client.get("", {"fields": "user.login,user.uid"})
        user = info.get("user") or {}
        login = user.get("login") or user.get("uid")
        if not login:
            raise AdapterAuthError("user_unknown")
        self._user_id = str(login)

    async def list(self, modules: Sequence[str]) -> AsyncIterator[RemoteDocument]:
        if disk_module.MODULE_DISK in set(modules):
            module = disk_module.YandexDiskModule(
                self._client, max_bytes=self._max_bytes
            )
            async for document in module.walk():
                yield document

    async def fetch(
        self, document: RemoteDocument, *, max_bytes: int
    ) -> FetchedContent:
        if document.external_id.startswith(disk_module.PREFIX):
            module = disk_module.YandexDiskModule(self._client, max_bytes=max_bytes)
            return await module.fetch(document, max_bytes=max_bytes)
        raise AdapterError("unknown_document")


def build_adapter(
    config: Mapping[str, str],
    credentials: Mapping[str, str],
    http: OutboundClient,
    settings: ConnectorSettings,
    *,
    recorder: Recorder | None = None,
) -> YandexAdapter:
    access = credentials.get("access_token")
    refresh = credentials.get("refresh_token")
    if not access:
        raise AdapterAuthError("credentials_missing")
    client_id = config.get("client_id", "")
    client_secret = credentials.get("client_secret", "")
    if not client_id or not client_secret:
        raise AdapterConfigError("app_credentials_missing")
    tokens = TokenSet(
        access_token=access,
        refresh_token=refresh or "",
        expires_at=to_int(credentials.get("expires_at")) or 0,
    )
    oauth = YandexOAuth(
        http,
        client_id=client_id,
        client_secret=client_secret,
        server=settings.yandex_oauth_server,
    )
    client = YandexDiskClient(
        http,
        api=settings.yandex_disk_api,
        auth=OAuthTokens(tokens, oauth),
        recorder=recorder,
    )
    return YandexAdapter(client, max_bytes=settings.max_document_bytes)


def register(registry: AdapterRegistry, settings: ConnectorSettings) -> None:
    def factory(
        spec: KindSpec,
        config: Mapping[str, str],
        credentials: Mapping[str, str],
        http: OutboundClient,
        options: AdapterOptions,
    ) -> YandexAdapter:
        return build_adapter(
            config, credentials, http, settings, recorder=options.recorder
        )

    def oauth(
        spec: KindSpec,
        config: Mapping[str, str],
        app_credentials: Mapping[str, str],
        http: OutboundClient,
    ) -> YandexOAuth:
        client_id = config.get("client_id", "")
        client_secret = app_credentials.get("client_secret", "")
        if not client_id or not client_secret:
            raise AdapterConfigError("app_credentials_missing")
        return YandexOAuth(
            http,
            client_id=client_id,
            client_secret=client_secret,
            server=settings.yandex_oauth_server,
        )

    registry.register(SPEC, factory, oauth)
