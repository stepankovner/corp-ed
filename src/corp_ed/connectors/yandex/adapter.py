"""Спецификация вида yandex360 и адаптер (режим per_user, OAuth Яндекс ID).

Админ регистрирует приложение на oauth.yandex.ru — тип «Для авторизации
пользователей», платформа «Веб-сервисы», адрес возврата — наша ручка
обратного вызова (тип «Для доступа к API» не подходит: его адрес
возврата фиксирован). Права: cloud_api:disk.read, cloud_api:disk.info,
для Вики — wiki:read. Смена прав приложения отзывает все его токены —
сотрудникам придётся подключиться заново. client_id — в config,
client_secret — в учётные данные подключения; каждый сотрудник
авторизует приложение сам.

Модули: disk — личный Диск сотрудника; shared_disks — общие диски
организации, к которым у сотрудника есть доступ; wiki — страницы Вики
из заданных разделов. Для shared_disks и wiki нужен идентификатор
организации (admin.yandex.ru → «Профиль организации»): API его
сотруднику не отдаёт.
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
from corp_ed.connectors.yandex import wiki as wiki_module
from corp_ed.connectors.yandex.disk import YandexDiskClient
from corp_ed.connectors.yandex.oauth import YandexAuth, YandexOAuth
from corp_ed.connectors.yandex.wiki import YandexWikiClient
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.outbound import OutboundClient
from corp_ed.domain.types import ConnectorMode

KIND = "yandex360"
_ORG_MODULES = frozenset({disk_module.MODULE_SHARED_DISKS, wiki_module.MODULE_WIKI})


def _check_config(config: Mapping[str, str]) -> str | None:
    org_id = config.get("org_id", "")
    if org_id and not org_id.isdigit():
        return "org_id_invalid"
    return None


SPEC = KindSpec(
    kind=KIND,
    title="Яндекс 360 (Диск и Вики)",
    mode=ConnectorMode.PER_USER,
    user_auth=UserAuth.OAUTH,
    modules=(
        ModuleSpec(disk_module.MODULE_DISK, "Яндекс Диск сотрудника"),
        ModuleSpec(
            disk_module.MODULE_SHARED_DISKS,
            "Общие диски организации (нужен ID организации)",
        ),
        ModuleSpec(wiki_module.MODULE_WIKI, "Яндекс Вики (нужен ID организации)"),
    ),
    config_fields=(
        FieldSpec("client_id", "ClientID приложения Яндекс ID"),
        FieldSpec(
            "org_id",
            "ID организации Яндекс 360 (admin.yandex.ru → Профиль организации)",
            required=False,
        ),
        FieldSpec(
            "wiki_roots",
            "Разделы Вики: slug или адреса через запятую (пусто — главная)",
            required=False,
        ),
    ),
    app_credential_fields=(
        FieldSpec("client_secret", "Client secret приложения Яндекс ID", secret=True),
    ),
    credential_fields=(),
    url_field=None,
    config_check=_check_config,
    extra={
        "app_scopes": "cloud_api:disk.read,cloud_api:disk.info,wiki:read",
        "app_type": "Для авторизации пользователей, платформа «Веб-сервисы»",
        "oauth_callback_path": OAUTH_CALLBACK_PATH,
    },
)


class YandexAdapter:
    def __init__(
        self,
        disk: YandexDiskClient,
        wiki: YandexWikiClient | None,
        *,
        max_bytes: int,
        org_id: str = "",
        wiki_roots: Sequence[str] = (),
    ) -> None:
        self._disk = disk
        self._wiki = wiki
        self._max_bytes = max_bytes
        self._org_id = org_id
        self._wiki_roots = tuple(wiki_roots)
        self._user_id: str | None = None

    @property
    def refreshed_credentials(self) -> Mapping[str, str] | None:
        return self._disk.refreshed_credentials

    @property
    def external_user_id(self) -> str | None:
        return self._user_id

    async def check(self) -> None:
        info = await self._disk.get("", {"fields": "user.login,user.uid"})
        user = info.get("user") or {}
        # uid — идентификатор, которым Яндекс 360 называет сотрудника в
        # своих API; логин может смениться.
        identity = user.get("uid") or user.get("login")
        if not identity:
            raise AdapterAuthError("user_unknown")
        self._user_id = str(identity)

    async def list(self, modules: Sequence[str]) -> AsyncIterator[RemoteDocument]:
        selected = set(modules)
        if selected & _ORG_MODULES and not self._org_id:
            # Админ выбрал модуль организации без её идентификатора:
            # вопрос к подключению, а не к грантам сотрудников.
            raise AdapterConfigError("org_id_missing")
        disk = disk_module.YandexDiskModule(
            self._disk, max_bytes=self._max_bytes, org_id=self._org_id
        )
        if disk_module.MODULE_DISK in selected:
            async for document in disk.walk():
                yield document
        if disk_module.MODULE_SHARED_DISKS in selected:
            async for document in disk.walk_shared():
                yield document
        if wiki_module.MODULE_WIKI in selected and self._wiki is not None:
            wiki = wiki_module.YandexWikiModule(
                self._wiki, roots=self._wiki_roots, max_bytes=self._max_bytes
            )
            async for document in wiki.walk():
                yield document

    async def fetch(
        self, document: RemoteDocument, *, max_bytes: int
    ) -> FetchedContent:
        if document.external_id.startswith(disk_module.PREFIX):
            module = disk_module.YandexDiskModule(self._disk, max_bytes=max_bytes)
            return await module.fetch(document, max_bytes=max_bytes)
        if document.external_id.startswith(wiki_module.PREFIX) and self._wiki:
            wiki = wiki_module.YandexWikiModule(
                self._wiki, roots=self._wiki_roots, max_bytes=max_bytes
            )
            return await wiki.fetch(document, max_bytes=max_bytes)
        raise AdapterError("unknown_document")


def _oauth(
    config: Mapping[str, str],
    app_credentials: Mapping[str, str],
    http: OutboundClient,
    settings: ConnectorSettings,
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
        redirect_uri=settings.oauth_callback_url,
    )


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
    tokens = TokenSet(
        access_token=access,
        refresh_token=refresh or "",
        expires_at=to_int(credentials.get("expires_at")) or 0,
    )
    auth = YandexAuth(tokens, _oauth(config, credentials, http, settings))
    org_id = config.get("org_id", "").strip()
    disk = YandexDiskClient(
        http, api=settings.yandex_disk_api, auth=auth, recorder=recorder
    )
    wiki = (
        YandexWikiClient(
            http,
            api=settings.yandex_wiki_api,
            org_id=org_id,
            auth=auth,
            recorder=recorder,
        )
        if org_id
        else None
    )
    return YandexAdapter(
        disk,
        wiki,
        max_bytes=settings.max_document_bytes,
        org_id=org_id,
        wiki_roots=wiki_module.parse_roots(config.get("wiki_roots", "")),
    )


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
        return _oauth(config, app_credentials, http, settings)

    registry.register(SPEC, factory, oauth)
