"""Спецификация вида bitrix24 и сборка адаптера.

Режим per_user с OAuth: админ регистрирует на портале локальное
приложение (scope disk и landing, адрес обработчика — наша ручка
обратного вызова), вводит адрес портала и client_id в config,
client_secret — в учётные данные подключения; каждый сотрудник
авторизует приложение сам. Вебхук (credentials["webhook"]) принимает
только cli connector-check: в форме API его нет.
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
from corp_ed.connectors.bitrix24 import disk as disk_module
from corp_ed.connectors.bitrix24 import knowledge_base as kb_module
from corp_ed.connectors.bitrix24 import notes as notes_module
from corp_ed.connectors.bitrix24.client import (
    MIN_INTERVAL,
    Bitrix24Client,
    OAuthAuth,
    Recorder,
    WebhookAuth,
)
from corp_ed.connectors.bitrix24.oauth import Bitrix24OAuth
from corp_ed.connectors.common import OAUTH_CALLBACK_PATH, TokenSet, to_int
from corp_ed.connectors.registry import (
    AdapterRegistry,
    FieldSpec,
    KindSpec,
    ModuleSpec,
    UserAuth,
)
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.outbound import OutboundClient
from corp_ed.domain.types import ConnectorMode

KIND = "bitrix24"
SPEC = KindSpec(
    kind=KIND,
    title="Битрикс24",
    mode=ConnectorMode.PER_USER,
    user_auth=UserAuth.OAUTH,
    modules=(
        ModuleSpec(disk_module.MODULE_DISK, "Общий диск и диски групп"),
        ModuleSpec(disk_module.MODULE_DISK_PERSONAL, "Мой диск сотрудника"),
        ModuleSpec(kb_module.MODULE_KNOWLEDGE_BASE, "База знаний"),
        ModuleSpec(
            notes_module.MODULE_KNOWLEDGE_BASE_V2, "База знаний 2.0 (scope note)"
        ),
    ),
    config_fields=(
        FieldSpec("portal", "Адрес портала (https://…bitrix24.ru/)"),
        FieldSpec("client_id", "Код приложения (client_id)"),
    ),
    app_credential_fields=(
        FieldSpec(
            "client_secret", "Секретный ключ приложения (client_secret)", secret=True
        ),
    ),
    credential_fields=(),
    url_field="portal",
    extra={
        "app_scopes": "disk,landing",
        "app_scopes_optional": "note — для модуля knowledge_base_v2",
        "oauth_callback_path": OAUTH_CALLBACK_PATH,
    },
)


class Bitrix24Adapter:
    def __init__(self, client: Bitrix24Client, *, max_bytes: int) -> None:
        self._client = client
        self._max_bytes = max_bytes
        self._user_id: str | None = None
        # Модуль базы знаний 2.0 держит содержимое между list и fetch.
        self._notes: notes_module.NotesModule | None = None

    @property
    def refreshed_credentials(self) -> Mapping[str, str] | None:
        return self._client.refreshed_credentials

    async def check(self) -> None:
        """profile — единственный метод без scope: кто мы на портале."""
        result = (await self._client.call("profile")).get("result")
        if not isinstance(result, dict) or not result.get("ID"):
            # Пустой объект — пользователь неактивен (документация).
            raise AdapterAuthError("user_inactive")
        self._user_id = str(result["ID"])

    @property
    def external_user_id(self) -> str | None:
        return self._user_id

    async def list(self, modules: Sequence[str]) -> AsyncIterator[RemoteDocument]:
        wanted = set(modules)
        if disk_module.MODULE_DISK_PERSONAL in wanted and self._user_id is None:
            await self.check()
        disk = disk_module.DiskModule(
            self._client, max_bytes=self._max_bytes, user_id=self._user_id
        )
        async for document in disk.walk(wanted):
            yield document
        if kb_module.MODULE_KNOWLEDGE_BASE in wanted:
            async for document in kb_module.KnowledgeBaseModule(self._client).walk():
                yield document
        if notes_module.MODULE_KNOWLEDGE_BASE_V2 in wanted:
            self._notes = notes_module.NotesModule(self._client)
            async for document in self._notes.walk():
                yield document

    async def fetch(
        self, document: RemoteDocument, *, max_bytes: int
    ) -> FetchedContent:
        if document.external_id.startswith(disk_module.PREFIX):
            disk = disk_module.DiskModule(
                self._client, max_bytes=max_bytes, user_id=self._user_id
            )
            return await disk.fetch(document, max_bytes=max_bytes)
        if document.external_id.startswith(kb_module.PREFIX):
            return await kb_module.KnowledgeBaseModule(self._client).fetch(document)
        if document.external_id.startswith(notes_module.PREFIX):
            if self._notes is None:
                self._notes = notes_module.NotesModule(self._client)
            return await self._notes.fetch(document)
        raise AdapterError("unknown_document")


def build_client(
    config: Mapping[str, str],
    credentials: Mapping[str, str],
    http: OutboundClient,
    settings: ConnectorSettings,
    *,
    recorder: Recorder | None = None,
    min_interval: float | None = None,
) -> Bitrix24Client:
    portal = config.get("portal", "")
    if not portal:
        raise AdapterConfigError("portal_missing")
    auth: WebhookAuth | OAuthAuth
    if credentials.get("webhook"):
        auth = WebhookAuth(credentials["webhook"])
    else:
        access = credentials.get("access_token")
        refresh = credentials.get("refresh_token")
        if not access or not refresh:
            raise AdapterAuthError("credentials_missing")
        client_id = config.get("client_id", "")
        client_secret = credentials.get("client_secret", "")
        if not client_id or not client_secret:
            raise AdapterConfigError("app_credentials_missing")
        auth = OAuthAuth(
            TokenSet(
                access_token=access,
                refresh_token=refresh,
                expires_at=to_int(credentials.get("expires_at")) or 0,
                member_id=credentials.get("member_id") or None,
            ),
            Bitrix24OAuth(
                http,
                portal=portal,
                client_id=client_id,
                client_secret=client_secret,
                server=settings.bitrix24_oauth_server,
            ),
        )
    return Bitrix24Client(
        http,
        portal=portal,
        auth=auth,
        recorder=recorder,
        min_interval=MIN_INTERVAL if min_interval is None else min_interval,
    )


def register(registry: AdapterRegistry, settings: ConnectorSettings) -> None:
    def factory(
        spec: KindSpec,
        config: Mapping[str, str],
        credentials: Mapping[str, str],
        http: OutboundClient,
        options: AdapterOptions,
    ) -> Bitrix24Adapter:
        client = build_client(
            config,
            credentials,
            http,
            settings,
            recorder=options.recorder,
            min_interval=0.0 if options.fast else None,
        )
        return Bitrix24Adapter(client, max_bytes=settings.max_document_bytes)

    def oauth(
        spec: KindSpec,
        config: Mapping[str, str],
        app_credentials: Mapping[str, str],
        http: OutboundClient,
    ) -> Bitrix24OAuth:
        client_id = config.get("client_id", "")
        client_secret = app_credentials.get("client_secret", "")
        if not config.get("portal") or not client_id or not client_secret:
            raise AdapterConfigError("app_credentials_missing")
        return Bitrix24OAuth(
            http,
            portal=config["portal"],
            client_id=client_id,
            client_secret=client_secret,
            server=settings.bitrix24_oauth_server,
        )

    registry.register(SPEC, factory, oauth)
