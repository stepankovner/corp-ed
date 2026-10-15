"""Спецификация вида r7office и адаптер (режим organization).

Р7-Офис Корпоративный сервер 2019 построен на ONLYOFFICE Workspace, и
его API документов совпадает с API ONLYOFFICE Community Server; пишем по
документации Р7 (support.r7-office.ru/community_server/api/): на
api.onlyoffice.com раздел Workspace уже снят (15.10). Корпоративный
сервер 2024 — другая система с другим API (`/api/v1/DocumentDirectory`,
без сведений о доступе), этим видом не поддерживается.

Админ компании вводит адрес портала, логин и пароль администратора
портала (или администратора модуля «Документы»). Модуль один —
«Общие документы» (`@common`): «Мои документы» сотрудников администратор
не видит, это не наше ограничение, а устройство Р7.

Права файла — по ответам `GET api/2.0/files/file/{id}/share` и
`.../folder/{id}/share` (правило — в rights.py): документ RESTRICTED с
явным списком почт тех, кто его читает. Почты — из `GET api/2.0/people`
(только активные, `status` 1), участники групп — из
`GET api/2.0/group/{id}` (у списка `people` документация называет
профиль частичным — на его `groups` права не строятся). «Все» — явный
список почт активных сотрудников портала, а не «вся компания»: в kronto
могут быть люди, которых нет в Р7. Администраторы портала и модуля
«Документы» читают все «Общие документы» (так решает сервер). Файл,
который не читает никто, не отдаётся; права файла не читаются
(403/404) — файл пропускается. Где правило не может различить свою
запись файла и унаследованную, оно решает в сторону закрытости: такой
файл может не дойти до части тех, кто видит его в Р7.

Идентификаторы групп «Все» (c5cc67d1-…) и «Администраторы» (cd84e66b-…),
имя модуля «documents» в listAdminModules, смысл поля `shared` («у
файла есть свои записи доступа») и правило сервера взяты из исходного
кода ONLYOFFICE Community Server, а не из документации Р7: сверить на
живой системе. Если сборка Р7 старше и `shared` в ответе нет — файл
считается `shared: true` (строже).

Папки и файлы подключённых сторонних хранилищ (`providerItem`) не
обходятся: их права живут у провайдера. Содержимое — подписанная ссылка
`GET api/2.0/files/file/{id}/presigneduri` (по ней файл отдаётся без
токена); форматы — те же, что у загрузки.
"""

from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import PurePath
from typing import Any
from urllib.parse import urljoin, urlsplit

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
    split_list,
    to_int,
)
from corp_ed.connectors.r7office.client import R7Client
from corp_ed.connectors.r7office.rights import (
    Ace,
    Person,
    can_read,
    group_subjects,
    parse_aces,
)
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

KIND = "r7office"
MODULE_COMMON = "common"
PREFIX = "file:"
COMMON_ROOT = "@common"
FOLDER_COMMON = 1
"""rootFolderType раздела «Общие документы» (FolderType.COMMON)."""
DOCUMENTS_MODULE = "documents"
MAX_DEPTH = 32
"""Вложенность папок от корня обхода. Глубже — tree_too_deep."""
MAX_ENTRIES = 20_000
"""Папок и файлов за обход. Больше — tree_too_large: сузить «Папки»."""
_SKIPPED = frozenset({"forbidden", "not_found"})

# Псевдонимы: в теле класса имя list занято методом листинга.
Crumbs = list[str]
_Subfolders = list[tuple[str, str]]
_Aces = list[Ace]


def _folder_ids(config: Mapping[str, str]) -> list[str]:
    return split_list(config.get("folders", ""))


def _check_config(config: Mapping[str, str]) -> str | None:
    if any(not folder.isdigit() for folder in _folder_ids(config)):
        return "folders_invalid"
    return None


SPEC = KindSpec(
    kind=KIND,
    title="Р7-Офис Корпоративный сервер 2019 (документы)",
    mode=ConnectorMode.ORGANIZATION,
    modules=(ModuleSpec(MODULE_COMMON, "Общие документы"),),
    config_fields=(
        FieldSpec("base_url", "Адрес портала Р7 (https://office.company.ru)"),
        FieldSpec(
            "folders",
            "ID папок в «Общих документах» по одному на строку или через запятую "
            "(пусто — весь раздел)",
            required=False,
            multiline=True,
        ),
    ),
    credential_fields=(
        FieldSpec("login", "Логин или почта администратора портала"),
        FieldSpec("password", "Пароль администратора портала", secret=True),
    ),
    url_field="base_url",
    config_check=_check_config,
    extra={"account": "администратор портала без двухфакторной проверки"},
    base=False,
    preview=True,
)


class R7OfficeAdapter:
    def __init__(
        self, client: R7Client, *, max_bytes: int, folders: Sequence[str] = ()
    ) -> None:
        self._client = client
        self._max_bytes = max_bytes
        self._folders = [f for f in folders if f]
        self._checked = False
        # Кеши на один запуск: сотрудники портала и составы групп.
        self._people: dict[str, Person] = {}
        self._groups: dict[str, frozenset[str]] = {}
        self._folder_aces: dict[str, _Aces | None] = {}
        self._entries = 0

    async def check(self) -> None:
        me = await self._client.get_object("people/@self")
        if not me.get("id"):
            raise AdapterAuthError("user_unknown")
        if not _is_admin(me):
            raise AdapterAuthError("admin_required")
        self._checked = True

    async def list(self, modules: Sequence[str]) -> AsyncIterator[RemoteDocument]:
        if MODULE_COMMON not in set(modules):
            return
        if not self._checked:
            await self.check()
        self._people = await self._load_people()
        self._groups.clear()
        self._folder_aces.clear()
        self._entries = 0
        roots = self._folders or [COMMON_ROOT]
        seen: set[str] = set()
        for root in roots:
            # Папка из настройки внутри другой из настройки — один обход.
            if root in seen:
                continue
            seen.add(root)
            async for document in self._walk(root, [], 0, seen):
                yield document

    async def fetch(
        self, document: RemoteDocument, *, max_bytes: int
    ) -> FetchedContent:
        if not document.external_id.startswith(PREFIX):
            raise AdapterError("unknown_document")
        if document.size is not None and document.size > max_bytes:
            raise AdapterError("document_too_large")
        file_id = document.external_id.removeprefix(PREFIX)
        link = await self._client.get(
            f"files/file/{path_segment(file_id)}/presigneduri", secret=True
        )
        if not isinstance(link, str) or not link:
            raise AdapterError("download_url_missing")
        data = await self._client.download(link, max_bytes=max_bytes)
        return FetchedFile(data=data, filename=document.filename or document.title)

    # --- обход ---------------------------------------------------------------------

    async def _walk(
        self, folder: str, path: Crumbs, depth: int, seen: set[str]
    ) -> AsyncIterator[RemoteDocument]:
        if depth > MAX_DEPTH:
            logger.warning("r7office_tree_too_deep", depth=depth)
            raise AdapterError("tree_too_deep")
        subfolders: _Subfolders = []
        here = path
        try:
            async for page in self._client.folder_pages(
                folder if folder == COMMON_ROOT else path_segment(folder)
            ):
                current = page.get("current")
                if not isinstance(current, dict):
                    raise AdapterError("unexpected_response")
                if to_int(current.get("rootFolderType")) != FOLDER_COMMON:
                    # Папка из настройки не в «Общих документах».
                    if depth == 0:
                        raise AdapterConfigError("folder_not_common")
                    return
                if depth == 0 and not here:
                    here = [str(current.get("title") or "")]
                for item in _items(page.get("folders")):
                    folder_id = _local_id(item)
                    if folder_id is None or folder_id in seen:
                        continue
                    seen.add(folder_id)
                    self._count()
                    subfolders.append((folder_id, str(item.get("title") or folder_id)))
                for item in _items(page.get("files")):
                    self._count()
                    document = await self._document(item, here)
                    if document is not None:
                        yield document
        except AdapterError as exc:
            if depth == 0 and exc.code == "not_found":
                raise AdapterConfigError("folder_not_found") from exc
            if depth == 0 or not _skippable(exc):
                raise
            # Папку удалили или закрыли посреди обхода — пропуск.
            logger.info("r7office_folder_skipped", code=exc.code)
            return
        for folder_id, title in subfolders:
            async for document in self._walk(
                folder_id, [*here, title], depth + 1, seen
            ):
                yield document

    def _count(self) -> None:
        self._entries += 1
        if self._entries > MAX_ENTRIES:
            logger.warning("r7office_tree_too_large", limit=MAX_ENTRIES)
            raise AdapterError("tree_too_large")

    async def _document(
        self, item: Mapping[str, Any], path: Crumbs
    ) -> RemoteDocument | None:
        file_id = _local_id(item)
        title = item.get("title")
        if file_id is None or not isinstance(title, str) or not title.strip():
            return None
        if to_int(item.get("rootFolderType")) not in (None, FOLDER_COMMON):
            return None
        key = f"{PREFIX}{file_id}"
        if PurePath(title).suffix.lower() not in supported_extensions():
            note_unsupported(title, key)
            return None
        size = to_int(item.get("pureContentLength"))
        if size is not None and size > self._max_bytes:
            note_too_large(key)
            return None
        folder_id = item.get("folderId")
        emails = await self._readers(
            file_id,
            str(folder_id) if folder_id is not None else None,
            shared=item.get("shared") is not False,
        )
        if not emails:
            return None
        updated = str(item.get("updated") or "")
        return RemoteDocument(
            external_id=key,
            title=title,
            url=self._web_url(item, file_id),
            version=f"{item.get('version') or ''}:{updated}",
            kind=RemoteDocumentKind.FILE,
            module=MODULE_COMMON,
            path="/".join(p for p in path if p),
            filename=title,
            size=size,
            modified_at=parse_datetime(updated),
            visibility=MaterialVisibility.RESTRICTED,
            allowed_emails=emails,
        )

    def _web_url(self, item: Mapping[str, Any], file_id: str) -> str:
        root = self._client.root
        link = item.get("webUrl")
        if isinstance(link, str) and link:
            url = urljoin(root, link)
            if urlsplit(url).hostname == urlsplit(root).hostname:
                return url
        # Адрес редактора ONLYOFFICE; сверить на живой системе.
        return f"{root}Products/Files/DocEditor.aspx?fileid={path_segment(file_id)}"

    # --- права ---------------------------------------------------------------------

    async def _load_people(self) -> dict[str, Person]:
        people: dict[str, Person] = {}
        async for user in self._client.pages("people"):
            user_id = user.get("id")
            email = user.get("email")
            if not user_id or not isinstance(email, str) or "@" not in email:
                continue
            if to_int(user.get("status")) != 1:
                # Заблокированные и удалённые не читают ничего.
                continue
            people[str(user_id).lower()] = Person(
                email=email.strip().lower(), admin=_is_admin(user)
            )
        return people

    async def _readers(
        self, file_id: str, folder_id: str | None, *, shared: bool
    ) -> frozenset[str]:
        file_aces = await self._aces(f"files/file/{path_segment(file_id)}/share")
        if file_aces is None:
            return frozenset()
        folder_aces: _Aces | None = None
        if shared and folder_id is not None:
            if folder_id not in self._folder_aces:
                self._folder_aces[folder_id] = await self._aces(
                    f"files/folder/{path_segment(folder_id)}/share"
                )
            folder_aces = self._folder_aces[folder_id]
        groups = {
            group: await self._group_members(group)
            for group in group_subjects([*file_aces, *(folder_aces or [])])
        }
        return frozenset(
            person.email
            for person_id, person in self._people.items()
            if can_read(
                person_id,
                person,
                file_aces,
                groups,
                shared=shared,
                folder_aces=folder_aces,
            )
        )

    async def _aces(self, path: str) -> _Aces | None:
        """Записи доступа; None — ответ не читается (403/404)."""
        try:
            return parse_aces(await self._client.get_list(path))
        except AdapterError as exc:
            if not _skippable(exc):
                raise
            logger.info("r7office_share_unreadable", code=exc.code)
            return None

    async def _group_members(self, group: str) -> frozenset[str]:
        if group not in self._groups:
            members: set[str] = set()
            try:
                info = await self._client.get_object(f"group/{path_segment(group)}")
            except AdapterError as exc:
                if not _skippable(exc):
                    raise
                logger.info("r7office_group_unreadable", code=exc.code)
            else:
                for member in _items(info.get("members")):
                    if member.get("id"):
                        members.add(str(member["id"]).lower())
            self._groups[group] = frozenset(members)
        return self._groups[group]


def _is_admin(user: Mapping[str, Any]) -> bool:
    modules = user.get("listAdminModules")
    return (
        user.get("isAdmin") is True
        or user.get("isOwner") is True
        or (isinstance(modules, list) and DOCUMENTS_MODULE in modules)
    )


def _items(value: Any) -> list[dict[str, Any]]:
    return (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


def _local_id(item: Mapping[str, Any]) -> str | None:
    """id папки или файла самого портала; стороннее хранилище — None."""
    if item.get("providerItem") is True:
        return None
    value = item.get("id")
    if isinstance(value, bool) or value is None:
        return None
    text = str(value)
    return text if text.isdigit() else None


def _skippable(exc: AdapterError) -> bool:
    return (
        not isinstance(exc, (AdapterAuthError, AdapterConfigError))
        and not exc.retryable
        and exc.code in _SKIPPED
    )


def build_adapter(
    config: Mapping[str, str],
    credentials: Mapping[str, str],
    http: OutboundClient,
    settings: ConnectorSettings,
    *,
    recorder: Recorder | None = None,
) -> R7OfficeAdapter:
    base_url = config.get("base_url", "").strip()
    if not base_url or urlsplit(base_url).scheme != "https":
        raise AdapterConfigError("base_url_invalid")
    if _check_config(config):
        raise AdapterConfigError("folders_invalid")
    login = credentials.get("login", "").strip()
    password = credentials.get("password", "")
    if not login or not password:
        raise AdapterAuthError("credentials_missing")
    return R7OfficeAdapter(
        R7Client(
            http, base_url=base_url, login=login, password=password, recorder=recorder
        ),
        max_bytes=settings.download_limit_bytes,
        folders=_folder_ids(config),
    )


def register(registry: AdapterRegistry, settings: ConnectorSettings) -> None:
    def factory(
        spec: KindSpec,
        config: Mapping[str, str],
        credentials: Mapping[str, str],
        http: OutboundClient,
        options: AdapterOptions,
    ) -> R7OfficeAdapter:
        return build_adapter(
            config, credentials, http, settings, recorder=options.recorder
        )

    registry.register(SPEC, factory)
