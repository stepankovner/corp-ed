"""Адаптер WebDAV-дисков: обход дерева сотрудника и скачивание файлов.

Обход — PROPFIND с Depth: 1 по каждой папке, начиная с корня сотрудника
или с папок из настройки «Папки». Пагинации в WebDAV нет, поэтому есть
потолки: глубина MAX_DEPTH и MAX_ENTRIES элементов на листинг одного
сотрудника. Упёрлись — ошибка tree_too_deep / tree_too_large, а не
молча обрезанный листинг: ядро считает дошедший до конца обход полным и
удалило бы всё, чего в нём не оказалось. Что делать — сузить «Папки».

Ссылкам из ответа не верим: элемент берётся, только если его href — тот
же хост и порт по https, без `.` и `..` (в том числе %2E%2E) и это
прямой потомок папки, которую листали. Чужой хост, соседняя папка, внук
или чужой корень пропускаются — запросов по ним не будет. Скачивание —
по адресу, собранному нами из проверенных сегментов, и без редиректов
на другой хост.

Инкрементальность — version = getetag + getlastmodified + размер: ядро
скачивает заново, только если версия сменилась; исчезнувшие файлы
удаляются после полного обхода. Скрытые и служебные файлы (имя с точки,
~$ — владельцы документов Office, корзины и миниатюры NAS) не читаются.
"""

import hashlib
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import PurePath
from urllib.parse import unquote, urljoin, urlsplit

import structlog

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterConfigError,
    AdapterError,
    FetchedFile,
    RemoteDocument,
)
from corp_ed.connectors.common import (
    Recorder,
    TokenSet,
    note_too_large,
    note_unsupported,
    path_segment,
    to_int,
)
from corp_ed.connectors.webdav.client import BasicAuth, DavAuth, Sleep, WebDavClient
from corp_ed.connectors.webdav.kinds import (
    FLAVORS,
    MAILRU_WEB,
    MAILRU_WEBDAV,
    MODULE_FILES,
    Flavor,
    parse_folders,
)
from corp_ed.connectors.webdav.multistatus import PRINCIPAL_BODY, DavEntry
from corp_ed.connectors.webdav.oauth import BearerAuth, NextcloudOAuth
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.outbound import OutboundClient
from corp_ed.domain.types import RemoteDocumentKind
from corp_ed.ingest.extract import supported_extensions

logger = structlog.get_logger()

MAX_DEPTH = 32
"""Вложенность папок от корня обхода. Глубже — tree_too_deep."""
MAX_ENTRIES = 20_000
"""Папок и файлов в листинге одного сотрудника (каждая папка — запрос).
Больше — tree_too_large: обход такого дерева раз в час — часы запросов."""
MAX_FILE_ID = 200
# Раскодированные сегменты пути от корня сервера. Псевдонимы — потому что
# в теле класса имя list занято методом листинга.
Segments = list[str]
_Children = list[tuple[str, DavEntry]]
_SKIPPED_FOLDER_CODES = frozenset({"forbidden", "not_found"})
# Корзины, снимки и миниатюры NAS: Synology (#recycle, #snapshot, @eaDir),
# QNAP (@Recycle, @Recently-Snapshot).
_SERVICE_FOLDERS = frozenset(
    {"#recycle", "#snapshot", "@eadir", "@recycle", "@recently-snapshot"}
)


class WebDavAdapter:
    def __init__(
        self,
        client: WebDavClient,
        *,
        kind: str,
        flavor: Flavor,
        server: str,
        login: str,
        user_id: str = "",
        folders: Sequence[tuple[str, ...]] = (),
        max_bytes: int,
    ) -> None:
        self._client = client
        self._kind = kind
        self._flavor = flavor
        self._server = server if server.endswith("/") else server + "/"
        parts = urlsplit(self._server)
        self._origin = f"https://{parts.netloc.lower()}"
        self._host = (parts.hostname or "").lower()
        self._port = parts.port or 443
        self._login = login
        self._user_id = user_id
        self._folders = list(folders)
        self._max_bytes = max_bytes
        self._root: Segments | None = None
        self._entries = 0

    @property
    def external_user_id(self) -> str | None:
        return self._user_id or self._login or None

    @property
    def refreshed_credentials(self) -> Mapping[str, str] | None:
        auth = self._client.auth
        return auth.refreshed_credentials if isinstance(auth, BearerAuth) else None

    async def check(self) -> None:
        root = await self._resolve_root()
        await self._client.propfind(self._url(root, (), collection=True), depth="0")

    async def list(self, modules: Sequence[str]) -> AsyncIterator[RemoteDocument]:
        if MODULE_FILES not in modules:
            return
        root = await self._resolve_root()
        self._entries = 0
        visited: set[tuple[str, ...]] = set()
        yielded: set[str] = set()
        for start in self._folders or [()]:
            async for document in self._walk(root, start, 0, visited, start=True):
                if document.external_id in yielded:
                    continue
                yielded.add(document.external_id)
                yield document

    async def fetch(self, document: RemoteDocument, *, max_bytes: int) -> FetchedFile:
        if document.size is not None and document.size > max_bytes:
            raise AdapterError("document_too_large")
        root = await self._resolve_root()
        # locator собран листингом этого же адаптера; чужой адрес — ошибка.
        if not document.locator.startswith(self._url(root, (), collection=True)):
            raise AdapterError("locator_invalid")
        data = await self._client.download(document.locator, max_bytes=max_bytes)
        return FetchedFile(data=data, filename=document.filename or document.title)

    # --- корень сотрудника ------------------------------------------------------

    async def _resolve_root(self) -> Segments:
        if self._root is not None:
            return self._root
        base = _segments(urlsplit(self._server).path)
        root = self._flavor.root
        if root == "nextcloud":
            uid = await self._discover_uid() or self._user_id or self._login
            if not uid:
                raise AdapterAuthError("user_unknown")
            self._user_id = uid
            self._root = [*base, "remote.php", "dav", "files", uid]
        elif root == "owncloud":
            self._root = [*base, "remote.php", "webdav"]
        else:
            self._root = base
        return self._root

    async def _discover_uid(self) -> str:
        """uid из current-user-principal: /remote.php/dav/principals/users/<uid>/.

        Чужой хост или другая форма пути — нет ответа (вернётся логин)."""
        base = _segments(urlsplit(self._server).path)
        entries = await self._client.propfind(
            self._url([*base, "remote.php", "dav"], (), collection=True),
            depth="0",
            body=PRINCIPAL_BODY,
        )
        for entry in entries:
            if not entry.principal:
                continue
            segments = self._own_segments(entry.principal, self._server)
            if segments is None or segments[: len(base)] != base:
                continue
            tail = segments[len(base) :]
            if tail[:4] == ["remote.php", "dav", "principals", "users"] and (
                len(tail) == 5
            ):
                return tail[4]
        return ""

    # --- обход ----------------------------------------------------------------

    async def _walk(
        self,
        root: Segments,
        folder: tuple[str, ...],
        depth: int,
        visited: set[tuple[str, ...]],
        *,
        start: bool = False,
    ) -> AsyncIterator[RemoteDocument]:
        if folder in visited:
            return
        visited.add(folder)
        if depth > MAX_DEPTH:
            logger.warning("webdav_tree_too_deep", kind=self._kind, depth=depth)
            raise AdapterError("tree_too_deep")
        url = self._url(root, folder, collection=True)
        try:
            entries = await self._client.propfind(url, depth="1")
        except AdapterAuthError as exc:
            if folder == () or exc.code != "auth_failed":
                raise
            # Apache mod_dav (и NAS на нём) отвечает на чужую папку 401, а
            # не 403, хотя пароль верный (стенд 09.10). Корень пускает —
            # закрытая папка; не пускает — пароль отозван посреди обхода.
            await self._client.propfind(self._url(root, (), collection=True), depth="0")
            logger.info(
                "webdav_folder_skipped",
                kind=self._kind,
                code="unauthorized",
                start=start,
            )
            return
        except AdapterError as exc:
            if (
                isinstance(exc, AdapterAuthError | AdapterConfigError)
                or exc.retryable
                or exc.code not in _SKIPPED_FOLDER_CODES
                # Корень сотрудника недоступен — это не папка, а учётка
                # или адрес: ошибка, а не пустой листинг.
                or folder == ()
            ):
                raise
            # Вложенная папка исчезла посреди обхода или закрыта; папки
            # из настройки у этого сотрудника может не быть вовсе.
            logger.info(
                "webdav_folder_skipped", kind=self._kind, code=exc.code, start=start
            )
            return
        children: _Children = []
        foreign = 0
        for entry in entries:
            name = self._child(entry.href, url, [*root, *folder])
            if name is None:
                foreign += 1
                continue
            self._entries += 1
            if self._entries > MAX_ENTRIES:
                logger.warning("webdav_tree_too_large", kind=self._kind)
                raise AdapterError("tree_too_large")
            children.append((name, entry))
        if foreign > 1:
            # Один «чужой» — сама папка; остальное сервер прислал зря.
            logger.info("webdav_hrefs_ignored", kind=self._kind, count=foreign - 1)
        for name, entry in children:
            if _hidden(name):
                continue
            if entry.is_collection:
                async for nested in self._walk(
                    root, (*folder, name), depth + 1, visited
                ):
                    yield nested
                continue
            document = self._document(root, folder, name, entry)
            if document is not None:
                yield document

    def _child(self, href: str, folder_url: str, folder: Segments) -> str | None:
        """Имя прямого потомка папки или None (сама папка, чужое, кривое)."""
        segments = self._own_segments(href, folder_url)
        if segments is None or len(segments) != len(folder) + 1:
            return None
        if segments[: len(folder)] != folder:
            return None
        return segments[-1]

    def _own_segments(self, href: str, base_url: str) -> Segments | None:
        """href → раскодированные сегменты пути, если он на нашем сервере.

        `.` и `..` отвергаются и в сыром виде (urljoin их схлопнул бы), и
        после раскодирования (%2E%2E); `/` внутри имени (%2F) — тоже."""
        raw = urlsplit(href).path
        if any(part in (".", "..") for part in raw.split("/")):
            return None
        parts = urlsplit(urljoin(base_url, href))
        if parts.scheme != "https" or (parts.hostname or "").lower() != self._host:
            return None
        if (parts.port or 443) != self._port:
            return None
        segments = [unquote(part) for part in parts.path.split("/") if part]
        if any(part in (".", "..") or "/" in part for part in segments):
            return None
        return segments

    def _document(
        self, root: Segments, folder: tuple[str, ...], name: str, entry: DavEntry
    ) -> RemoteDocument | None:
        external_id = self._external_id(entry, (*folder, name))
        if PurePath(name).suffix.lower() not in supported_extensions():
            note_unsupported(name, external_id)
            return None
        if entry.size is not None and entry.size > self._max_bytes:
            note_too_large(external_id)
            return None
        locator = self._url(root, (*folder, name))
        return RemoteDocument(
            external_id=external_id,
            title=name,
            url=self._web_url(entry, folder, locator),
            version=f"{entry.etag}:{entry.modified_raw}:{entry.size or ''}",
            kind=RemoteDocumentKind.FILE,
            module=MODULE_FILES,
            path="/".join(folder),
            locator=locator,
            filename=name,
            size=entry.size,
            modified_at=entry.modified,
        )

    def _external_id(self, entry: DavEntry, path: tuple[str, ...]) -> str:
        if self._flavor.stable_ids and 0 < len(entry.file_id) <= MAX_FILE_ID:
            return f"{self._kind}:id:{entry.file_id}"
        # Без сквозного номера файл — свой у каждого сотрудника: общий
        # путь у двух учёток ещё не значит общий файл.
        owner = _digest((self._user_id or self._login).lower())[:16]
        return f"{self._kind}:u:{owner}:{_digest('/'.join(path))[:40]}"

    def _web_url(self, entry: DavEntry, folder: tuple[str, ...], locator: str) -> str:
        web = self._flavor.web
        if web == "fileid" and entry.file_id and len(entry.file_id) <= MAX_FILE_ID:
            return f"{self._server}index.php/f/{path_segment(entry.file_id)}"
        if web == "mailru":
            inner = "/".join(path_segment(part) for part in folder)
            return f"{MAILRU_WEB}{inner}/" if inner else MAILRU_WEB
        return locator

    def _url(
        self, root: Segments, path: Sequence[str], *, collection: bool = False
    ) -> str:
        segments = [*root, *path]
        encoded = "/".join(path_segment(part) for part in segments)
        url = f"{self._origin}/{encoded}"
        if collection and segments:
            url += "/"
        return url


def _segments(path: str) -> list[str]:
    return [unquote(part) for part in path.split("/") if part]


def _hidden(name: str) -> bool:
    return name.startswith((".", "~$")) or name.lower() in _SERVICE_FOLDERS


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# --- сборка ---------------------------------------------------------------------


def build_adapter(
    kind: str,
    config: Mapping[str, str],
    credentials: Mapping[str, str],
    http: OutboundClient,
    settings: ConnectorSettings,
    *,
    sleep: Sleep | None = None,
    recorder: Recorder | None = None,
) -> WebDavAdapter:
    flavor = FLAVORS[kind]
    server = MAILRU_WEBDAV if flavor.root == "mailru" else config.get("server", "")
    if not server:
        raise AdapterConfigError("server_missing")
    try:
        folders = parse_folders(config.get("folders", ""))
    except ValueError as exc:
        raise AdapterConfigError(str(exc)) from exc
    auth: DavAuth
    login = credentials.get("login", "")
    user_id = credentials.get("user_id", "")
    if credentials.get("access_token"):
        client_id = config.get("client_id", "")
        client_secret = credentials.get("client_secret", "")
        if not client_id or not client_secret:
            raise AdapterConfigError("app_credentials_missing")
        auth = BearerAuth(
            TokenSet(
                access_token=credentials["access_token"],
                refresh_token=credentials.get("refresh_token", ""),
                expires_at=to_int(credentials.get("expires_at")) or 0,
            ),
            NextcloudOAuth(
                http, server=server, client_id=client_id, client_secret=client_secret
            ),
            user_id=user_id,
        )
    elif login and credentials.get("password"):
        auth = BasicAuth(login, credentials["password"])
    else:
        raise AdapterAuthError("credentials_missing")
    client = (
        WebDavClient(http, auth=auth, recorder=recorder)
        if sleep is None
        else WebDavClient(http, auth=auth, sleep=sleep, recorder=recorder)
    )
    return WebDavAdapter(
        client,
        kind=kind,
        flavor=flavor,
        server=server,
        login=login,
        user_id=user_id,
        folders=folders,
        max_bytes=settings.max_document_bytes,
    )
