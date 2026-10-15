"""Спецификация вида m365 и адаптер (режим organization).

Подключение — приложение в Entra ID самой компании (Регистрация
приложений): application permissions Microsoft Graph `Sites.Read.All`,
`Files.Read.All`, `User.Read.All`, `GroupMember.Read.All` с согласием
администратора и секрет клиента. В форме kronto — ID клиента (tenant),
ID приложения (client) и секрет; токен — client credentials (auth.py).

Модули:
- sharepoint — все сайты (`GET /sites`, без личных `isPersonalSite`) и их
  подсайты (`/sites/{id}/sites`), у каждого — библиотеки документов
  (`/sites/{id}/drives`, driveType = documentLibrary; системные Graph по
  умолчанию не отдаёт);
- onedrive — OneDrive каждого включённого сотрудника-участника клиента
  (`/users`, `/users/{id}/drive`; нет лицензии или OneDrive не создан —
  404, сотрудник пропускается). Владелец диска читает свои файлы всегда.

Обход библиотеки — `/drives/{id}/root/delta` без токена: полный перечень
текущего состояния страницами `@odata.nextLink` (scan-guidance: только
delta гарантирует, что ни один элемент не потерян при записи во время
обхода). `parentReference.path` delta не отдаёт — путь собирается по id
папок; один элемент может прийти дважды — берётся последний; удалённые
(`deleted`), папки, корень, пакеты (записные книжки OneNote) и ярлыки на
чужие файлы (`remoteItem`) — не документы. Ядро состояния адаптера между
запусками не хранит, поэтому deltaLink не сохраняется: каждый запуск —
полный перечень, исчезнувшее из него ядро удаляет.

Версия — `eTag` и `cTag` (cTag меняется с содержимым, но delta OneDrive
для бизнеса его не отдаёт при создании и изменении — поэтому опорой
служит eTag). Права — на каждый файл отдельным
`/drives/{d}/items/{i}/permissions` (раскрыть их в delta нельзя, а ядро
переносит права каждый запуск): дорого по лимитам Graph для больших
библиотек, разбор — access.py.

Содержимое — `/drives/{d}/items/{i}/content` (302 на подписанный адрес
SharePoint, client.py). Документы Word, Excel, PowerPoint лежат в
SharePoint обычными файлами docx/xlsx/pptx и скачиваются как есть.

Доступ только к выбранным сайтам (Sites.Selected): вместо
`Sites.Read.All` и `Files.Read.All` приложению дают `Sites.Selected`, а
администратор SharePoint выдаёт ему чтение конкретных сайтов
(`POST /sites/{id}/permissions` с ролью read — для этого вызова нужно
отдельное приложение с Sites.FullControl.All, обычно
`Grant-PnPAzureADAppSitePermission`; permissions-selected-overview). В
поле «Сайты» перечисляются их адреса: адаптер берёт сайты по адресу
(`/sites/{host}:/{путь}`) вместо `GET /sites`, который с Sites.Selected
недоступен. Модуль onedrive в этом режиме не работает (личные сайты не
выданы), сайт группы для групп SharePoint не находится
(`/groups/{id}/sites/root` требует Sites.Read.All) — их участники прав не
получат (в сторону закрытости). Поле «Сайты» можно заполнить и при
Sites.Read.All — тогда обходятся только эти сайты.
"""

import asyncio
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any
from urllib.parse import unquote, urlsplit

import structlog

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterConfigError,
    AdapterError,
    AdapterOptions,
    FetchedContent,
    FetchedFile,
    RemoteDocument,
)
from corp_ed.connectors.common import (
    Recorder,
    note_too_large,
    note_unsupported,
    parse_datetime,
    path_segment,
    to_int,
)
from corp_ed.connectors.m365.access import (
    AccessResolver,
    collection_key,
    user_email,
)
from corp_ed.connectors.m365.auth import LOGIN_HOST, ClientCredentials
from corp_ed.connectors.m365.client import GRAPH_ROOT, GraphClient, Sleep
from corp_ed.connectors.registry import (
    AdapterRegistry,
    FieldSpec,
    KindSpec,
    ModuleSpec,
)
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.outbound import OutboundClient
from corp_ed.domain.types import ConnectorMode, MaterialVisibility, RemoteDocumentKind
from corp_ed.ingest.extract import supported_extensions

logger = structlog.get_logger()

KIND = "m365"
MODULE_SHAREPOINT = "sharepoint"
MODULE_ONEDRIVE = "onedrive"
PREFIX = "m365:"
PERMISSIONS = (
    "Sites.Read.All",
    "Files.Read.All",
    "User.Read.All",
    "GroupMember.Read.All",
)
USERS_PAGE = 999
MAX_SITES = 10_000
MAX_SITE_DEPTH = 8
MAX_DRIVE_ITEMS = 200_000
MAX_PATH_DEPTH = 64
MAX_CONFIGURED_SITES = 50
_SITE_FIELDS = "id,name,displayName,webUrl,isPersonalSite"
_DRIVE_FIELDS = "id,name,driveType,webUrl"
_DELTA_FIELDS = (
    "id,name,eTag,cTag,size,webUrl,lastModifiedDateTime,parentReference,"
    "file,folder,package,root,deleted,remoteItem"
)
# Ошибки одного сайта, диска или файла, после которых обход продолжается.
_SKIPPABLE = frozenset({"forbidden", "not_found"})
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_DOMAIN = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)
_SITE_SPLIT = re.compile(r"[\s,;]+")


def _site_urls(value: str) -> list[str]:
    return [item for item in _SITE_SPLIT.split(value.strip()) if item]


def _site_path(url: str) -> str | None:
    """Адрес сайта → путь Graph `sites/{host}:/{путь}`; None — не адрес
    сайта SharePoint Online."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").casefold()
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.query
        or parts.fragment
        or parts.port not in (None, 443)
        or not host.endswith(".sharepoint.com")
        or not _DOMAIN.match(host)
    ):
        return None
    segments = [unquote(s) for s in parts.path.split("/") if s]
    if any(s in {".", ".."} for s in segments):
        return None
    if not segments:
        return f"sites/{host}"
    return f"sites/{host}:/" + "/".join(path_segment(s) for s in segments)


def _check_config(config: Mapping[str, str]) -> str | None:
    tenant = config.get("tenant_id", "").strip().casefold()
    if not (_GUID.match(tenant) or _DOMAIN.match(tenant)):
        return "tenant_id_invalid"
    if not _GUID.match(config.get("client_id", "").strip().casefold()):
        return "client_id_invalid"
    sites = _site_urls(config.get("sites", ""))
    if len(sites) > MAX_CONFIGURED_SITES or any(_site_path(s) is None for s in sites):
        return "sites_invalid"
    return None


def _check_credentials(credentials: Mapping[str, str]) -> str | None:
    secret = credentials.get("client_secret", "")
    if not secret.strip() or any(ch.isspace() for ch in secret.strip()):
        return "client_secret_invalid"
    return None


SPEC = KindSpec(
    kind=KIND,
    title="Microsoft 365: SharePoint и OneDrive",
    mode=ConnectorMode.ORGANIZATION,
    modules=(
        ModuleSpec(MODULE_SHAREPOINT, "SharePoint: сайты и библиотеки документов"),
        ModuleSpec(MODULE_ONEDRIVE, "OneDrive сотрудников (обход дольше)"),
    ),
    config_fields=(
        FieldSpec(
            "tenant_id", "ID клиента (tenant) Entra ID или домен onmicrosoft.com"
        ),
        FieldSpec("client_id", "ID приложения (client) из регистрации в Entra ID"),
        FieldSpec(
            "sites",
            "Адреса сайтов SharePoint через запятую: пусто — все сайты; "
            "обязательно, если приложению выдан Sites.Selected",
            required=False,
        ),
    ),
    credential_fields=(
        FieldSpec("client_secret", "Секрет клиента приложения (значение)", secret=True),
    ),
    url_field=None,
    config_check=_check_config,
    credentials_check=_check_credentials,
    extra={
        "permissions": ",".join(PERMISSIONS),
        "consent": "Entra ID → Регистрация приложений → API permissions → "
        "Microsoft Graph → Application permissions: эти разрешения и "
        "«Grant admin consent»; Certificates & secrets → New client secret",
    },
    base=False,
    preview=True,
)


@dataclass(frozen=True)
class _Drive:
    id: str
    name: str
    web_url: str


# Псевдонимы нужны методам адаптера: внутри класса имя list — его метод.
Sites = list[Mapping[str, Any]]
Drives = list[_Drive]


@dataclass(frozen=True, slots=True)
class _Entry:
    """Элемент delta без лишнего: библиотека держится в памяти целиком."""

    name: str
    parent: str | None
    is_file: bool
    etag: str
    ctag: str
    size: int | None
    web_url: str
    modified: str


class M365Adapter:
    def __init__(
        self,
        client: GraphClient,
        *,
        site_urls: Sequence[str],
        max_bytes: int,
    ) -> None:
        self._client = client
        self._site_urls = list(site_urls)
        self._access = AccessResolver(client)
        self._max_bytes = max_bytes

    async def check(self) -> None:
        await self._client.auth.token()
        if self._site_urls:
            for url in self._site_urls:
                path = _site_path(url)
                if path is None:
                    raise AdapterConfigError("sites_invalid")
                try:
                    await self._client.get(path, {"$select": "id"})
                except AdapterError as exc:
                    if exc.code in _SKIPPABLE:
                        raise AdapterConfigError("site_unavailable") from exc
                    raise
        else:
            await self._probe("sites", {"$select": "id"}, "permission_missing_sites")
        await self._probe(
            "users", {"$select": "id", "$top": 1}, "permission_missing_users"
        )
        await self._probe(
            "groups", {"$select": "id", "$top": 1}, "permission_missing_groups"
        )

    async def list(self, modules: Sequence[str]) -> AsyncIterator[RemoteDocument]:
        selected = set(modules)
        if MODULE_SHAREPOINT in selected:
            async for document in self._sharepoint():
                yield document
        if MODULE_ONEDRIVE in selected:
            async for document in self._onedrive():
                yield document

    async def fetch(
        self, document: RemoteDocument, *, max_bytes: int
    ) -> FetchedContent:
        drive_id, _, item_id = document.external_id.removeprefix(PREFIX).partition("/")
        if not document.external_id.startswith(PREFIX) or not drive_id or not item_id:
            raise AdapterError("unknown_document")
        if document.size is not None and document.size > max_bytes:
            raise AdapterError("document_too_large")
        data = await self._client.download(
            f"drives/{path_segment(drive_id)}/items/{path_segment(item_id)}/content",
            max_bytes=max_bytes,
        )
        return FetchedFile(data=data, filename=document.filename or document.title)

    # --- SharePoint -----------------------------------------------------------------

    async def _sharepoint(self) -> AsyncIterator[RemoteDocument]:
        seen_drives: set[str] = set()
        for site in await self._sites():
            site_id = str(site.get("id") or "")
            title = str(site.get("displayName") or site.get("name") or site_id)
            drives = await self._site_drives(site_id)
            for drive in drives:
                if drive.id in seen_drives:
                    continue
                seen_drives.add(drive.id)
                documents = self._drive_documents(
                    drive,
                    MODULE_SHAREPOINT,
                    f"SharePoint/{title}/{drive.name}",
                    collection=collection_key(site_id),
                    owner=None,
                )
                async for document in self._guarded(documents):
                    yield document

    async def _sites(self) -> Sites:
        """Сайты для обхода: заданные адресами или все, с подсайтами."""
        queue: list[tuple[Mapping[str, Any], int]] = []
        if self._site_urls:
            for url in self._site_urls:
                path = _site_path(url)
                if path is None:
                    raise AdapterConfigError("sites_invalid")
                try:
                    configured = await self._client.get(path, {"$select": _SITE_FIELDS})
                except AdapterError as exc:
                    _reraise(exc)
                    # Сайт удалён или больше не выдан приложению: его
                    # документы уйдут из kronto вместе с ним.
                    logger.warning("m365_site_unavailable", code=exc.code)
                    continue
                queue.append((configured, 0))
        else:
            try:
                async for site in self._client.pages(
                    "sites", {"$select": _SITE_FIELDS}
                ):
                    queue.append((site, 0))
            except AdapterError as exc:
                if exc.code == "forbidden":
                    raise AdapterConfigError("permission_missing_sites") from exc
                raise
        found: dict[str, Mapping[str, Any]] = {}
        while queue:
            current, depth = queue.pop(0)
            site_id = str(current.get("id") or "")
            if (
                not site_id
                or site_id.casefold() in found
                or current.get("isPersonalSite")
            ):
                continue
            if len(found) >= MAX_SITES:
                raise AdapterError("too_many_sites")
            found[site_id.casefold()] = current
            if depth >= MAX_SITE_DEPTH:
                continue
            try:
                async for child in self._client.pages(
                    f"sites/{path_segment(site_id)}/sites", {"$select": _SITE_FIELDS}
                ):
                    queue.append((child, depth + 1))
            except AdapterError as exc:
                _reraise(exc)
                logger.info("m365_subsites_unreadable", code=exc.code)
        return [*found.values()]

    async def _site_drives(self, site_id: str) -> Drives:
        drives: Drives = []
        try:
            async for drive in self._client.pages(
                f"sites/{path_segment(site_id)}/drives", {"$select": _DRIVE_FIELDS}
            ):
                if drive.get("driveType") != "documentLibrary" or not drive.get("id"):
                    continue
                drives.append(_drive(drive))
        except AdapterError as exc:
            _reraise(exc)
            logger.warning("m365_site_drives_unreadable", code=exc.code)
            return []
        return drives

    # --- OneDrive -------------------------------------------------------------------

    async def _onedrive(self) -> AsyncIterator[RemoteDocument]:
        owners: list[tuple[str, str]] = []
        try:
            async for user in self._client.pages(
                "users",
                {
                    "$select": "id,mail,userPrincipalName,accountEnabled,userType",
                    "$top": USERS_PAGE,
                },
            ):
                if (
                    user.get("accountEnabled") is False
                    or user.get("userType") == "Guest"
                ):
                    continue
                user_id = str(user.get("id") or "")
                email = user_email(user)
                if user_id and email:
                    owners.append((user_id, email))
        except AdapterError as exc:
            if exc.code == "forbidden":
                raise AdapterConfigError("permission_missing_users") from exc
            raise
        opened = forbidden = 0
        for user_id, email in owners:
            try:
                raw = await self._client.get(
                    f"users/{path_segment(user_id)}/drive", {"$select": _DRIVE_FIELDS}
                )
            except AdapterError as exc:
                # 404 — у сотрудника нет OneDrive (лицензии, не создан).
                _reraise(exc)
                logger.info("m365_onedrive_missing", code=exc.code)
                forbidden += exc.code == "forbidden"
                continue
            opened += 1
            if not raw.get("id"):
                continue
            drive = _drive(raw)
            documents = self._drive_documents(
                drive,
                MODULE_ONEDRIVE,
                f"OneDrive/{email}",
                collection=None,
                owner=email,
            )
            async for document in self._guarded(documents):
                yield document
        if forbidden and not opened:
            # Ни один OneDrive не открылся: у приложения нет Files.Read.All,
            # а не у всех сотрудников разом нет дисков.
            raise AdapterConfigError("permission_missing_files")

    # --- общее ----------------------------------------------------------------------

    async def _probe(self, path: str, params: Mapping[str, Any], code: str) -> None:
        try:
            await self._client.get(path, params)
        except AdapterError as exc:
            if exc.code == "forbidden":
                raise AdapterConfigError(code) from exc
            raise

    async def _guarded(
        self, documents: AsyncIterator[RemoteDocument]
    ) -> AsyncIterator[RemoteDocument]:
        """Обход одной библиотеки. Недоступна с самого начала (удалена, не
        выдана приложению) — пропуск. Оборвалась посреди — повтор запуска
        позже: по неполному листингу ядро удалило бы то, что не успело
        встретиться."""
        yielded = False
        try:
            async for document in documents:
                yielded = True
                yield document
        except AdapterError as exc:
            _reraise(exc)
            if yielded:
                raise AdapterError("drive_interrupted", retryable=True) from exc
            logger.warning("m365_drive_skipped", code=exc.code)

    async def _drive_documents(
        self,
        drive: _Drive,
        module: str,
        label: str,
        *,
        collection: str | None,
        owner: str | None,
    ) -> AsyncIterator[RemoteDocument]:
        entries = await self._delta(drive.id)
        for item_id, entry in entries.items():
            if not entry.is_file:
                continue
            document = self._document(drive, item_id, entry, module)
            if document is None:
                continue
            try:
                permissions = [
                    p
                    async for p in self._client.pages(
                        f"drives/{path_segment(drive.id)}/items/"
                        f"{path_segment(item_id)}/permissions"
                    )
                ]
            except AdapterError as exc:
                _reraise(exc)
                # Файл удалён между перечнем и запросом или права не
                # читаются: без прав документ не отдаётся.
                logger.info("m365_permissions_unreadable", code=exc.code)
                continue
            readers = await self._access.readers(permissions, collection=collection)
            if owner:
                readers = readers | {owner}
            yield _with_access(document, _path(label, entry, entries), readers)

    async def _delta(self, drive_id: str) -> dict[str, _Entry]:
        entries: dict[str, _Entry] = {}
        async for item in self._client.pages(
            f"drives/{path_segment(drive_id)}/root/delta", {"$select": _DELTA_FIELDS}
        ):
            item_id = str(item.get("id") or "")
            if not item_id:
                continue
            if item.get("deleted") is not None or item.get("root") is not None:
                entries.pop(item_id, None)
                continue
            parent = item.get("parentReference")
            parent_id = parent.get("id") if isinstance(parent, Mapping) else None
            is_file = (
                isinstance(item.get("file"), Mapping)
                and item.get("package") is None
                and item.get("remoteItem") is None
            )
            is_folder = isinstance(item.get("folder"), Mapping)
            if not is_file and not is_folder:
                entries.pop(item_id, None)
                continue
            # Повтор элемента — последнее состояние (driveitem-delta, Remarks).
            entries.pop(item_id, None)
            entries[item_id] = _Entry(
                name=str(item.get("name") or item_id),
                parent=str(parent_id) if parent_id else None,
                is_file=is_file,
                etag=str(item.get("eTag") or ""),
                ctag=str(item.get("cTag") or ""),
                size=to_int(item.get("size")),
                web_url=str(item.get("webUrl") or ""),
                modified=str(item.get("lastModifiedDateTime") or ""),
            )
            if len(entries) > MAX_DRIVE_ITEMS:
                raise AdapterError("tree_too_large")
        return entries

    def _document(
        self, drive: _Drive, item_id: str, entry: _Entry, module: str
    ) -> RemoteDocument | None:
        key = f"{drive.id}/{item_id}"
        if PurePath(entry.name).suffix.lower() not in supported_extensions():
            note_unsupported(entry.name, key)
            return None
        if entry.size is not None and entry.size > self._max_bytes:
            note_too_large(key)
            return None
        url = entry.web_url if entry.web_url.startswith("https://") else drive.web_url
        return RemoteDocument(
            external_id=f"{PREFIX}{key}",
            title=entry.name,
            url=url,
            version=f"{entry.etag}|{entry.ctag}",
            kind=RemoteDocumentKind.FILE,
            module=module,
            filename=entry.name,
            size=entry.size,
            modified_at=parse_datetime(entry.modified),
        )


def _drive(raw: Mapping[str, Any]) -> _Drive:
    drive_id = str(raw.get("id") or "")
    url = str(raw.get("webUrl") or "")
    return _Drive(
        id=drive_id,
        name=str(raw.get("name") or drive_id),
        web_url=url if url.startswith("https://") else "",
    )


def _path(label: str, entry: _Entry, entries: Mapping[str, _Entry]) -> str:
    """Папки над файлом по id родителей (delta не отдаёт parentReference.path)."""
    names: list[str] = []
    current = entry.parent
    seen: set[str] = set()
    while current and current not in seen and len(names) < MAX_PATH_DEPTH:
        seen.add(current)
        folder = entries.get(current)
        if folder is None or folder.is_file:
            break
        names.append(folder.name)
        current = folder.parent
    return "/".join([label, *reversed(names)])


def _with_access(
    document: RemoteDocument, path: str, readers: frozenset[str]
) -> RemoteDocument:
    """Всегда RESTRICTED с явным списком: «вся компания» Graph не сообщает
    ни одним документированным полем (ссылки — не доступ, access.py)."""
    return RemoteDocument(
        external_id=document.external_id,
        title=document.title,
        url=document.url,
        version=document.version,
        kind=document.kind,
        module=document.module,
        path=path,
        locator=document.locator,
        filename=document.filename,
        size=document.size,
        modified_at=document.modified_at,
        visibility=MaterialVisibility.RESTRICTED,
        allowed_emails=readers,
    )


def _reraise(exc: AdapterError) -> None:
    if exc.retryable or isinstance(exc, AdapterAuthError) or exc.code not in _SKIPPABLE:
        raise exc


def build_adapter(
    config: Mapping[str, str],
    credentials: Mapping[str, str],
    http: OutboundClient,
    settings: ConnectorSettings,
    *,
    recorder: Recorder | None = None,
    sleep: Sleep = asyncio.sleep,
    login_host: str = LOGIN_HOST,
    graph_root: str = GRAPH_ROOT,
) -> M365Adapter:
    secret = credentials.get("client_secret", "").strip()
    if not secret:
        raise AdapterAuthError("credentials_missing")
    tenant = config.get("tenant_id", "").strip()
    client_id = config.get("client_id", "").strip()
    if not tenant or not client_id:
        raise AdapterConfigError("app_settings_missing")
    sites = _site_urls(config.get("sites", ""))
    if any(_site_path(url) is None for url in sites):
        raise AdapterConfigError("sites_invalid")
    auth = ClientCredentials(
        http,
        tenant=tenant,
        client_id=client_id,
        client_secret=secret,
        login_host=login_host,
        sleep=sleep,
    )
    client = GraphClient(http, auth, root=graph_root, sleep=sleep, recorder=recorder)
    return M365Adapter(client, site_urls=sites, max_bytes=settings.download_limit_bytes)


def register(registry: AdapterRegistry, settings: ConnectorSettings) -> None:
    def factory(
        spec: KindSpec,
        config: Mapping[str, str],
        credentials: Mapping[str, str],
        http: OutboundClient,
        options: AdapterOptions,
    ) -> M365Adapter:
        return build_adapter(
            config, credentials, http, settings, recorder=options.recorder
        )

    registry.register(SPEC, factory)
