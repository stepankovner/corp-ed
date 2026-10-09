"""Спецификация вида gdrive и адаптер (режим organization).

Подключение — сервисный аккаунт в Google Cloud самой компании с
делегированием на домен (domain-wide delegation): админ Workspace
выдаёт его client ID три scope только на чтение, kronto подписывает JWT
закрытым ключом и получает токены от имени сотрудников домена. Своего
OAuth-приложения у kronto нет: `drive.readonly` — «restricted scope»,
публичному приложению нужна платная проверка Google. Поэтому только
Google Workspace; личные аккаунты Gmail не поддерживаются.

Модули:
- shared_drives — все общие диски домена: список — от имени
  администратора (`drives.list?useDomainAdminAccess=true`), файлы — от
  имени участника диска (files.list не принимает useDomainAdminAccess):
  организатор из домена, иначе участник с наибольшей ролью, иначе
  участник группы-участника. Диск без участников из домена пропускается.
  Права у элементов общих дисков files.list не отдаёт: состав диска —
  один permissions.list на диск, отдельный вызов на файл — только если на
  нём или на папке над ним есть свои разрешения
  (`hasAugmentedPermissions`) или папка с ограниченным доступом;
- user_drives — «Мой диск» каждого активного сотрудника (Directory
  `users.list`): файлы, которыми он владеет (`'me' in owners`), — каждый
  файл попадает в обход ровно один раз, у владельца; права приходят в
  листинге. Дороже по квоте: токен и листинг на каждого сотрудника.

Форматы: Google Документы, Таблицы, Презентации — экспорт в docx, xlsx,
pptx (`files/{id}/export`, лимит экспорта Google — 10 МБ, больше —
document_too_large); прочие типы Google (формы, рисунки, сайты) —
«пропущенный формат»; ярлыки и папки — не документы; обычные файлы —
`alt=media`, расширение — из имени или, если его нет, из MIME-типа.

Изменения: полный листинг на каждый запуск, версия — modifiedTime и
md5Checksum (у документов Google — номер версии): изменённое ядро
скачивает заново, исчезнувшее из полного листинга (удалено, в корзине,
доступ потерян) — удаляет. Changes API не нужен: ядро не хранит
состояние адаптера между запусками, а листинг метаданных дешевле
скачивания.
"""

import asyncio
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

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
from corp_ed.connectors.gdrive.access import (
    AccessResolver,
    Audience,
    direct,
    opening,
)
from corp_ed.connectors.gdrive.auth import (
    DRIVE_SCOPE,
    GROUPS_SCOPE,
    SCOPES,
    USERS_SCOPE,
    ServiceAccountAuth,
    SubjectRejectedError,
    key_problem,
    parse_key,
)
from corp_ed.connectors.gdrive.client import GoogleClient, Sleep
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

KIND = "gdrive"
MODULE_SHARED = "shared_drives"
MODULE_USERS = "user_drives"
PREFIX = "gdrive:"
FILES_PAGE = 1000
DRIVES_PAGE = 100
PERMISSIONS_PAGE = 100
USERS_PAGE = 500
MAX_DEPTH = 64
KEY_FIELD_LENGTH = 8192

FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"
_GOOGLE_APPS = "application/vnd.google-apps."
# Документы Google → формат, который читает ассистент.
EXPORTS = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
}
# Расширение файла без расширения в имени — по MIME-типу.
_MIME_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "text/plain": ".txt",
    "text/markdown": ".md",
    **{mime: ext for mime, ext in (value for value in EXPORTS.values())},
}
_FILE_FIELDS = (
    "id,name,mimeType,parents,modifiedTime,md5Checksum,version,size,"
    "webViewLink,hasAugmentedPermissions,inheritedPermissionsDisabled"
)
_PERMISSION_FIELDS = "id,type,role,emailAddress,domain,deleted,view"
_PERMISSION_DETAILS = f"{_PERMISSION_FIELDS},permissionDetails(inherited)"
_ROLE_RANK = {
    "organizer": 0,
    "owner": 0,
    "fileOrganizer": 1,
    "writer": 2,
    "commenter": 3,
    "reader": 4,
}
# Ошибки одного файла или диска, после которых обход продолжается.
_SKIPPABLE = frozenset({"forbidden", "not_found"})
_DOMAIN = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+$")


def _domains(value: str) -> list[str]:
    return [d.strip().casefold() for d in value.split(",") if d.strip()]


def _check_config(config: Mapping[str, str]) -> str | None:
    domains = _domains(config.get("domains", ""))
    if not domains or not all(_DOMAIN.match(d) for d in domains):
        return "domains_invalid"
    admin = config.get("admin_email", "").strip()
    if not _EMAIL.match(admin):
        return "admin_email_invalid"
    if admin.rsplit("@", 1)[-1].casefold() not in domains:
        return "admin_email_domain_mismatch"
    return None


SPEC = KindSpec(
    kind=KIND,
    title="Google Диск (Google Workspace)",
    mode=ConnectorMode.ORGANIZATION,
    modules=(
        ModuleSpec(MODULE_SHARED, "Общие диски"),
        ModuleSpec(
            MODULE_USERS,
            "Диски сотрудников (файлы, которыми владеет каждый; обход дольше)",
        ),
    ),
    config_fields=(
        FieldSpec("domains", "Домены Google Workspace через запятую (company.ru)"),
        FieldSpec(
            "admin_email",
            "Почта администратора Workspace: от его имени читаются список "
            "дисков, сотрудники и группы",
        ),
    ),
    credential_fields=(
        FieldSpec(
            "service_account_key",
            "JSON-ключ сервисного аккаунта с делегированием на домен",
            secret=True,
            max_length=KEY_FIELD_LENGTH,
        ),
    ),
    url_field=None,
    config_check=_check_config,
    credentials_check=lambda credentials: key_problem(
        credentials.get("service_account_key", "")
    ),
    extra={
        "scopes": ",".join(SCOPES),
        "delegation": "Консоль администратора Google → Безопасность → Доступ к "
        "данным и управление ими → Управление API → Делегирование домена: "
        "client_id из JSON-ключа и эти scope",
    },
    preview=True,
    base=True,
)


@dataclass(frozen=True)
class _Folder:
    id: str
    name: str
    parent: str | None
    augmented: bool | None
    limited: bool
    permissions: tuple[Mapping[str, Any], ...] | None = None


# Папки над файлом, от ближней к корню; None — цепочка не известна.
# Псевдоним нужен методам адаптера: внутри класса имя list — его метод.
Chain = list[_Folder] | None


class GoogleDriveAdapter:
    def __init__(
        self,
        client: GoogleClient,
        *,
        admin: str,
        domains: Sequence[str],
        max_bytes: int,
    ) -> None:
        self._client = client
        self._admin = admin
        self._access = AccessResolver(client, admin=admin, domains=domains)
        self._max_bytes = max_bytes
        # Открывающие папок с ограниченным доступом — на запуск.
        self._openings: dict[str, Audience] = {}

    async def check(self) -> None:
        try:
            await self._client.drive(
                self._admin,
                "drives",
                {"useDomainAdminAccess": "true", "pageSize": 1, "fields": "drives(id)"},
            )
        except AdapterError as exc:
            if exc.code == "forbidden":
                # Почта не администратора домена: общих дисков всех не
                # увидеть, состав групп не прочитать.
                raise AdapterConfigError("admin_required") from exc
            raise
        # Состав групп нужен правам любого модуля: делегирование проверяется
        # сразу, а не первой синхронизацией.
        await self._client.auth.token(self._admin, GROUPS_SCOPE)

    async def list(self, modules: Sequence[str]) -> AsyncIterator[RemoteDocument]:
        selected = set(modules)
        if MODULE_SHARED in selected:
            async for document in self._shared_drives():
                yield document
        if MODULE_USERS in selected:
            async for document in self._user_drives():
                yield document

    async def fetch(
        self, document: RemoteDocument, *, max_bytes: int
    ) -> FetchedContent:
        if not document.external_id.startswith(PREFIX):
            raise AdapterError("unknown_document")
        file_id = document.external_id.removeprefix(PREFIX)
        export_mime, _, subject = document.locator.partition("|")
        if not subject:
            raise AdapterError("locator_missing")
        filename = document.filename or document.title
        if export_mime:
            data = await self._client.download(
                subject,
                f"files/{path_segment(file_id)}/export",
                {"mimeType": export_mime},
                max_bytes=max_bytes,
            )
            return FetchedFile(data=data, filename=filename)
        if document.size is not None and document.size > max_bytes:
            raise AdapterError("document_too_large")
        data = await self._client.download(
            subject,
            f"files/{path_segment(file_id)}",
            {"alt": "media", "supportsAllDrives": "true"},
            max_bytes=max_bytes,
        )
        return FetchedFile(data=data, filename=filename)

    # --- общие диски --------------------------------------------------------------

    async def _shared_drives(self) -> AsyncIterator[RemoteDocument]:
        drives = [
            drive
            async for drive in self._client.drive_pages(
                self._admin,
                "drives",
                {
                    "useDomainAdminAccess": "true",
                    "pageSize": DRIVES_PAGE,
                    "fields": "nextPageToken,drives(id,name)",
                },
                "drives",
            )
        ]
        for drive in drives:
            drive_id = str(drive.get("id") or "")
            if not drive_id:
                continue
            name = str(drive.get("name") or drive_id)
            members = await self._permissions(self._admin, drive_id, admin_access=True)
            subject = await self._drive_subject(members or [])
            if members is None or subject is None:
                # Читать не от чьего имени: участников из домена нет.
                logger.warning("gdrive_drive_skipped", drive_id=drive_id)
                continue
            audience = await self._access.audience(members)
            try:
                async for document in self._drive_files(
                    drive_id, f"Общие диски/{name}", subject, audience
                ):
                    yield document
            except SubjectRejectedError as exc:
                # Читающего заблокировали посреди обхода: листинг диска
                # неполон, удалять по нему нельзя — повтор, читающий будет
                # выбран заново.
                await self._ensure_admin()
                raise AdapterError("drive_reader_rejected", retryable=True) from exc

    async def _drive_subject(self, members: Sequence[Mapping[str, Any]]) -> str | None:
        """От чьего имени читать файлы диска: организатор из домена, потом
        участники по убыванию роли, потом участники групп-участников."""
        ranked = sorted(
            (p for p in members if not p.get("deleted") and not p.get("view")),
            key=lambda p: _ROLE_RANK.get(str(p.get("role")), 9),
        )
        candidates: list[str] = []
        for p in ranked:
            email = str(p.get("emailAddress") or "").casefold()
            if p.get("type") == "user" and self._access.in_domain(email):
                candidates.append(email)
        for p in ranked:
            email = str(p.get("emailAddress") or "").casefold()
            if p.get("type") == "group" and email:
                group = await self._access.group(email)
                candidates.extend(
                    sorted(e for e in group.emails if self._access.in_domain(e))
                )
        for email in dict.fromkeys(candidates):
            try:
                await self._client.auth.token(email, DRIVE_SCOPE)
            except SubjectRejectedError:
                # Сотрудник заблокирован или удалён — следующий.
                await self._ensure_admin()
                continue
            return email
        return None

    async def _ensure_admin(self) -> None:
        """Отказ в токене сотруднику — это он ушёл, или отозван сам ключ?
        Ключ отозван — отказ подключению (поднимается наверх), а не
        «пропустить всех» с удалением их документов в конце запуска."""
        await self._client.auth.token(self._admin, DRIVE_SCOPE, renew=True)

    async def _drive_files(
        self, drive_id: str, label: str, subject: str, members: Audience
    ) -> AsyncIterator[RemoteDocument]:
        listing = {
            "corpora": "drive",
            "driveId": drive_id,
            "includeItemsFromAllDrives": "true",
            "supportsAllDrives": "true",
            "pageSize": FILES_PAGE,
        }
        folders = await self._folders(subject, listing, drive_id)
        async for item in self._client.drive_pages(
            subject,
            "files",
            {
                **listing,
                "q": f"mimeType != '{FOLDER}' and trashed = false",
                "fields": f"nextPageToken,files({_FILE_FIELDS})",
            },
            "files",
        ):
            document = self._document(item, MODULE_SHARED, subject)
            if document is None:
                continue
            chain = _chain(item, folders, root=drive_id)
            try:
                audience = await self._shared_audience(item, chain, subject, members)
            except AdapterError as exc:
                if exc.retryable or isinstance(exc, AdapterAuthError):
                    raise
                if exc.code not in _SKIPPABLE:
                    raise
                logger.info("gdrive_file_skipped", code=exc.code)
                continue
            yield _with_access(document, _path(label, chain), audience)

    async def _shared_audience(
        self,
        item: Mapping[str, Any],
        chain: Chain,
        subject: str,
        members: Audience,
    ) -> Audience:
        """Права файла общего диска. Нет своих разрешений ни на файле, ни
        на папках над ним — читают участники диска; иначе — permissions.list."""
        plain = (
            chain is not None
            and item.get("hasAugmentedPermissions") is False
            and all(f.augmented is False and not f.limited for f in chain)
        )
        if plain:
            return members
        file_id = str(item.get("id"))
        permissions = await self._permissions(subject, file_id) or []
        audience = await self._access.audience(permissions)
        for folder in _limited(chain):
            opened = await self._opening(subject, folder)
            audience = (audience & opened) | await self._access.audience(
                direct(permissions)
            )
        return audience

    # --- диски сотрудников ----------------------------------------------------------

    async def _user_drives(self) -> AsyncIterator[RemoteDocument]:
        users = self._client.directory_pages(
            self._admin,
            USERS_SCOPE,
            "users",
            {
                "customer": "my_customer",
                "maxResults": USERS_PAGE,
                "orderBy": "email",
                "fields": "nextPageToken,users(primaryEmail,suspended,archived)",
            },
            "users",
        )
        emails = [
            str(user.get("primaryEmail")).casefold()
            async for user in users
            if user.get("primaryEmail")
            and not user.get("suspended")
            and not user.get("archived")
        ]
        for email in emails:
            try:
                async for document in self._owned_files(email):
                    yield document
            except SubjectRejectedError:
                # Удалён или заблокирован между списком и обменом токена.
                await self._ensure_admin()
                logger.info("gdrive_user_skipped")
                continue

    async def _owned_files(self, email: str) -> AsyncIterator[RemoteDocument]:
        listing = {"corpora": "user", "pageSize": FILES_PAGE}
        owned = "'me' in owners and trashed = false"
        folders = await self._folders(email, listing, None, owned=owned)
        label = f"Диски сотрудников/{email}"
        async for item in self._client.drive_pages(
            email,
            "files",
            {
                **listing,
                "q": f"{owned} and mimeType != '{FOLDER}'",
                "fields": (
                    f"nextPageToken,files({_FILE_FIELDS},"
                    f"permissions({_PERMISSION_DETAILS}))"
                ),
            },
            "files",
        ):
            document = self._document(item, MODULE_USERS, email)
            if document is None:
                continue
            chain = _chain(item, folders, root=None)
            try:
                audience = await self._owned_audience(item, chain, email)
            except AdapterError as exc:
                if exc.retryable or isinstance(exc, AdapterAuthError):
                    raise
                if exc.code not in _SKIPPABLE:
                    raise
                logger.info("gdrive_file_skipped", code=exc.code)
                continue
            yield _with_access(document, _path(label, chain), audience)

    async def _owned_audience(
        self, item: Mapping[str, Any], chain: Chain, email: str
    ) -> Audience:
        raw = item.get("permissions")
        permissions = (
            raw
            if isinstance(raw, list)
            else await self._permissions(email, str(item.get("id"))) or []
        )
        audience = await self._access.audience(permissions)
        for folder in _limited(chain):
            opened = await self._opening(email, folder)
            audience = (audience & opened) | await self._access.audience(
                direct(permissions)
            )
        return audience

    # --- общее ------------------------------------------------------------------

    async def _folders(
        self,
        subject: str,
        listing: Mapping[str, Any],
        root: str | None,
        *,
        owned: str = "trashed = false",
    ) -> dict[str, _Folder]:
        """Все папки диска заранее: путь и права файла собираются по
        цепочке родителей без запроса на каждую папку."""
        folders: dict[str, _Folder] = {}
        with_permissions = root is None
        fields = _FILE_FIELDS
        if with_permissions:
            fields = f"{fields},permissions({_PERMISSION_DETAILS})"
        async for item in self._client.drive_pages(
            subject,
            "files",
            {
                **listing,
                "q": f"{owned} and mimeType = '{FOLDER}'",
                "fields": f"nextPageToken,files({fields})",
            },
            "files",
        ):
            folder_id = str(item.get("id") or "")
            if not folder_id:
                continue
            parents = item.get("parents")
            raw = item.get("permissions")
            folders[folder_id] = _Folder(
                id=folder_id,
                name=str(item.get("name") or folder_id),
                parent=str(parents[0])
                if isinstance(parents, list) and parents
                else None,
                augmented=_flag(item.get("hasAugmentedPermissions")),
                limited=bool(item.get("inheritedPermissionsDisabled")),
                permissions=tuple(raw) if isinstance(raw, list) else None,
            )
        return folders

    async def _permissions(
        self, subject: str, file_id: str, *, admin_access: bool = False
    ) -> Sequence[Mapping[str, Any]] | None:
        """permissions.list; None — элемент недоступен (403/404)."""
        params: dict[str, Any] = {
            "supportsAllDrives": "true",
            "pageSize": PERMISSIONS_PAGE,
            "fields": f"nextPageToken,permissions({_PERMISSION_DETAILS})",
        }
        if admin_access:
            params["useDomainAdminAccess"] = "true"
        try:
            return [
                p
                async for p in self._client.drive_pages(
                    subject,
                    f"files/{path_segment(file_id)}/permissions",
                    params,
                    "permissions",
                )
            ]
        except AdapterError as exc:
            if exc.retryable or isinstance(exc, AdapterAuthError):
                raise
            if exc.code not in _SKIPPABLE:
                raise
            logger.info("gdrive_permissions_unreadable", code=exc.code)
            if admin_access:
                return None
            raise

    async def _opening(self, subject: str, folder: _Folder) -> Audience:
        if folder.id in self._openings:
            return self._openings[folder.id]
        permissions: Sequence[Mapping[str, Any]] | None = folder.permissions
        if permissions is None:
            permissions = await self._permissions(subject, folder.id) or []
        opened = await self._access.audience(opening(permissions))
        self._openings[folder.id] = opened
        return opened

    def _document(
        self, item: Mapping[str, Any], module: str, subject: str
    ) -> RemoteDocument | None:
        file_id = str(item.get("id") or "")
        name = str(item.get("name") or file_id)
        mime = str(item.get("mimeType") or "")
        if not file_id or mime in (FOLDER, SHORTCUT):
            return None
        export_mime = ""
        filename: str | None
        size = to_int(item.get("size"))
        if mime in EXPORTS:
            export_mime, extension = EXPORTS[mime]
            filename = name if name.lower().endswith(extension) else name + extension
            # size документа Google — не размер экспорта: лимит проверит
            # сам экспорт (exportSizeLimitExceeded).
            size = None
        elif mime.startswith(_GOOGLE_APPS):
            note_unsupported(f"{name}.{mime.removeprefix(_GOOGLE_APPS)}", file_id)
            return None
        else:
            filename = _filename(name, mime)
            if filename is None:
                note_unsupported(name, file_id)
                return None
            if size is not None and size > self._max_bytes:
                note_too_large(file_id)
                return None
        modified = str(item.get("modifiedTime") or "")
        stamp = item.get("md5Checksum") or item.get("version") or ""
        url = item.get("webViewLink")
        return RemoteDocument(
            external_id=f"{PREFIX}{file_id}",
            title=name,
            url=url
            if isinstance(url, str) and url.startswith("https://")
            else f"https://drive.google.com/open?id={file_id}",
            version=f"{modified}:{stamp}",
            kind=RemoteDocumentKind.FILE,
            module=module,
            locator=f"{export_mime}|{subject}",
            filename=filename,
            size=size,
            modified_at=parse_datetime(modified),
        )


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _filename(name: str, mime: str) -> str | None:
    supported = supported_extensions()
    if PurePath(name).suffix.lower() in supported:
        return name
    extension = _MIME_EXTENSIONS.get(mime.split(";", 1)[0].strip().lower())
    if extension and extension in supported:
        return name + extension
    return None


def _chain(
    item: Mapping[str, Any], folders: Mapping[str, _Folder], *, root: str | None
) -> list[_Folder] | None:
    """Папки от ближней к корню. None — какая-то папка не известна (в
    общем диске это значит: права по цепочке не вывести)."""
    chain: list[_Folder] = []
    parents = item.get("parents")
    current = str(parents[0]) if isinstance(parents, list) and parents else None
    seen: set[str] = set()
    while current and current != root and len(chain) < MAX_DEPTH:
        if current in seen:
            break
        seen.add(current)
        folder = folders.get(current)
        if folder is None:
            # Мой диск: корень и чужие папки не в листинге владельца —
            # цепочка просто кончается. Общий диск: неизвестная папка.
            return chain if root is None else None
        chain.append(folder)
        current = folder.parent
    return chain


def _limited(chain: list[_Folder] | None) -> list[_Folder]:
    """Папки с ограниченным доступом в цепочке."""
    return [f for f in chain or [] if f.limited]


def _path(label: str, chain: list[_Folder] | None) -> str:
    names = [f.name for f in reversed(chain or [])]
    return "/".join([label, *names])


def _with_access(
    document: RemoteDocument, path: str, audience: Audience
) -> RemoteDocument:
    if audience.everyone:
        visibility, emails = MaterialVisibility.TENANT, frozenset[str]()
    else:
        visibility, emails = MaterialVisibility.RESTRICTED, audience.emails
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
        visibility=visibility,
        allowed_emails=emails,
    )


def build_adapter(
    config: Mapping[str, str],
    credentials: Mapping[str, str],
    http: OutboundClient,
    settings: ConnectorSettings,
    *,
    recorder: Recorder | None = None,
    sleep: Sleep = asyncio.sleep,
) -> GoogleDriveAdapter:
    raw_key = credentials.get("service_account_key", "")
    if not raw_key:
        raise AdapterAuthError("credentials_missing")
    try:
        key = parse_key(raw_key)
    except ValueError as exc:
        raise AdapterAuthError(str(exc)) from exc
    admin = config.get("admin_email", "").strip().casefold()
    if not admin:
        raise AdapterConfigError("admin_email_missing")
    domains = _domains(config.get("domains", ""))
    if not domains:
        raise AdapterConfigError("domains_missing")
    auth = ServiceAccountAuth(
        http, key, token_url=settings.google_token_url, sleep=sleep
    )
    client = GoogleClient(
        http,
        auth,
        drive_api=settings.google_drive_api,
        directory_api=settings.google_directory_api,
        sleep=sleep,
        recorder=recorder,
    )
    return GoogleDriveAdapter(
        client, admin=admin, domains=domains, max_bytes=settings.max_document_bytes
    )


def register(registry: AdapterRegistry, settings: ConnectorSettings) -> None:
    def factory(
        spec: KindSpec,
        config: Mapping[str, str],
        credentials: Mapping[str, str],
        http: OutboundClient,
        options: AdapterOptions,
    ) -> GoogleDriveAdapter:
        return build_adapter(
            config, credentials, http, settings, recorder=options.recorder
        )

    registry.register(SPEC, factory)
