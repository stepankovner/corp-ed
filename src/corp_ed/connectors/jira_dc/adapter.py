"""Спецификация вида jira_dc и адаптер (режим organization).

Обход: проекты (ключи из config или все, что видит владелец токена) →
задачи проекта поиском JQL (`/search`, startAt/maxResults, порядок по
дате создания) → для каждой — читатели. Полный листинг на каждом
запуске: ядро удаляет то, чего в листинге нет, поэтому инкрементальный
`updated >= <прошлый запуск>` здесь невозможен; горизонт вида
`updated >= -365d` админ задаёт сам фильтром JQL.

Читатели — ответ самой Jira: `/user/permission/search?permissions=BROWSE
&issueKey=…&username=` — активные пользователи, которые могут открыть
задачу, с учётом схемы прав проекта (группы, роли, «любой», автор,
исполнитель, поля-пользователи) и уровня безопасности задачи. Почты —
из тех же ответов; пользователь без почты не получает ничего.
Кеш — на запуск и только там, где ответ не зависит от задачи: схема
прав проекта выдаёт Browse лишь «статичным» держателям (STATIC_HOLDERS)
и у задачи нет уровня безопасности — тогда один ответ на проект.
Иначе — запрос на задачу (состав уровней безопасности REST DC не
отдаёт, а автор и исполнитель у каждой задачи свои). Схема прав не
прочиталась — тоже на задачу: дороже, но верно.

Поиск пользователей с правом Jira отдаёт только администратору (Jira
или проекта; живой стенд 10.3.26, 15.10): проект, где токену ответили
403, пропускается целиком; ни одного ответа и хотя бы один пропуск —
AdapterConfigError("admin_required"). Почему не схема прав, роли и
группы по отдельности: состав уровней безопасности REST DC не отдаёт
вовсе, а `/group/member` и схема прав — тоже только администратору;
ответ Jira о задаче учитывает всё сразу.

Содержимое (fetch): задача с `expand=renderedFields` — HTML описания и
комментариев от самой Jira; комментарии с ограничением видимости
(роль, группа) отбрасываются: администратор их видит, а читатели
задачи — не все.

Вложения — `fields.attachment` из того же поиска: читатели задачи,
скачивание по `content` только с хоста Jira.
"""

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
    FetchedPage,
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
from corp_ed.connectors.jira_dc.client import JiraClient
from corp_ed.connectors.jira_dc.render import issue_html, visible_comments
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

KIND = "jira_dc"
MODULE_ISSUES = "issues"
MODULE_ATTACHMENTS = "attachments"
ISSUE_PREFIX = "issue:"
ATTACHMENT_PREFIX = "att:"
SEARCH_LIMIT = 100
USERS_LIMIT = 1000
"""Страница поиска пользователей с правом: больше 1000 Jira не отдаёт."""
COMMENTS_LIMIT = 100
MAX_USER_PAGES = 200
"""200 страниц по 1000 — 200 000 пользователей: дальше — сбой, а не
бесконечный обход."""
BROWSE = "BROWSE"
"""Ключ права в поиске пользователей — старый: BROWSE_PROJECTS Jira 10.3
отвергает (400 «Invalid permission name», стенд 15.10)."""
STATIC_HOLDERS = frozenset(
    {"anyone", "group", "user", "projectRole", "applicationRole", "projectLead"}
)
"""Держатели права Browse, чей состав не зависит от задачи. Автор,
исполнитель, поля-пользователи и незнакомые типы — зависят."""
_SEARCH_FIELDS = "summary,updated,created,security,attachment,project"
_ISSUE_FIELDS = (
    "summary,description,comment,project,issuetype,status,reporter,assignee,updated"
)
_PROJECT_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,254}$")
_ORDER_BY = re.compile(r"\border\s+by\b", re.IGNORECASE)
_SKIPPABLE = frozenset({"forbidden", "not_found"})


def _config_check(config: Mapping[str, str]) -> str | None:
    if any(not _PROJECT_KEY.match(key) for key in _project_keys(config)):
        return "projects_invalid"
    if _ORDER_BY.search(config.get("jql", "")):
        return "jql_order_by"
    return None


SPEC = KindSpec(
    kind=KIND,
    title="Jira Software Server / Data Center (задачи)",
    mode=ConnectorMode.ORGANIZATION,
    modules=(
        ModuleSpec(MODULE_ISSUES, "Задачи: описание и комментарии"),
        ModuleSpec(
            MODULE_ATTACHMENTS, "Вложения задач (docx, doc, xlsx, pptx, pdf, txt, md)"
        ),
    ),
    config_fields=(
        FieldSpec("base_url", "Адрес Jira (https://jira.company.ru/)"),
        FieldSpec(
            "projects", "Ключи проектов через запятую (пусто — все доступные)", False
        ),
        FieldSpec(
            "jql",
            "Фильтр JQL сверх проектов, без ORDER BY (например: updated >= -365d)",
            False,
        ),
    ),
    credential_fields=(
        FieldSpec(
            "token",
            "Персональный токен доступа администратора Jira (или администратора "
            "нужных проектов)",
            secret=True,
        ),
    ),
    url_field="base_url",
    config_check=_config_check,
    extra={"token_owner": "администратор Jira или администратор нужных проектов"},
    preview=True,
)


@dataclass
class _Project:
    key: str
    name: str
    static: bool
    """Ответ о читателях один на проект (без уровня безопасности)."""
    readers: frozenset[str] | None = None
    """Почты из кеша проекта; None — ещё не спрашивали."""


class _ProjectSkippedError(Exception):
    """Поиск пользователей с правом в проекте недоступен токену (403)."""


class JiraAdapter:
    def __init__(
        self,
        client: JiraClient,
        *,
        projects: Sequence[str],
        jql: str,
        max_bytes: int,
    ) -> None:
        self._client = client
        self._projects = [p for p in projects if p]
        self._jql = jql.strip()
        self._max_bytes = max_bytes
        self._no_email: set[str] = set()
        """Логины пользователей без почты: им не выдаётся ничего."""
        self._answered = 0
        """Сколько раз Jira ответила, кто читает задачу, за этот обход."""

    @property
    def root(self) -> str:
        return self._client.root

    async def check(self) -> None:
        user = await self._client.get("myself")
        if not isinstance(user, dict) or not user.get("name"):
            raise AdapterAuthError("anonymous")
        if user.get("active") is False:
            raise AdapterAuthError("user_inactive")

    async def list(self, modules: Sequence[str]) -> AsyncIterator[RemoteDocument]:
        wanted = set(modules) & {MODULE_ISSUES, MODULE_ATTACHMENTS}
        if not wanted:
            return
        skipped = 0
        self._no_email = set()
        self._answered = 0
        async for raw in self._iter_projects():
            project = _Project(
                key=str(raw["key"]),
                name=str(raw.get("name") or raw["key"]),
                static=await self._static(str(raw["key"])),
            )
            try:
                async for document in self._project_documents(project, wanted):
                    yield document
            except _ProjectSkippedError:
                skipped += 1
                logger.warning(
                    "jira_project_skipped", project=project.key, code="admin_required"
                )
        if self._no_email:
            logger.info("jira_users_without_email", count=len(self._no_email))
        if skipped and not self._answered:
            # Ни в одном проекте токен не может узнать читателей задач (а
            # где ответов не понадобилось, задач ему не видно): без этого
            # прав не выдать никому — это настройка, а не сбой.
            raise AdapterConfigError("admin_required")

    async def fetch(
        self, document: RemoteDocument, *, max_bytes: int
    ) -> FetchedContent:
        if document.external_id.startswith(ISSUE_PREFIX):
            issue_id = document.external_id.removeprefix(ISSUE_PREFIX)
            return await self._fetch_issue(issue_id, max_bytes=max_bytes)
        if document.external_id.startswith(ATTACHMENT_PREFIX):
            attachment_id = document.external_id.removeprefix(ATTACHMENT_PREFIX)
            if document.size is not None and document.size > max_bytes:
                raise AdapterError("document_too_large")
            link = document.locator
            if not link:
                info = await self._client.get(
                    f"attachment/{path_segment(attachment_id)}"
                )
                link = str(info.get("content") or "")
            if not link:
                raise AdapterError("download_url_missing")
            data = await self._client.download(link, max_bytes=max_bytes)
            return FetchedFile(data=data, filename=document.filename or document.title)
        raise AdapterError("unknown_document")

    # --- проекты и задачи -----------------------------------------------------------

    async def _iter_projects(self) -> AsyncIterator[dict[str, Any]]:
        if self._projects:
            for key in self._projects:
                try:
                    project = await self._client.get(f"project/{path_segment(key)}")
                except AdapterError as exc:
                    _reraise_unless(exc, _SKIPPABLE)
                    # Проекта нет или он закрыт токену: админ увидит это по
                    # нулю задач, а не по сбою всего запуска.
                    logger.warning("jira_project_missing", project=key, code=exc.code)
                    continue
                if isinstance(project, dict) and project.get("key"):
                    yield project
            return
        projects = await self._client.get("project")
        for project in projects if isinstance(projects, list) else []:
            found = project.get("key") if isinstance(project, dict) else None
            if isinstance(found, str) and _PROJECT_KEY.match(found):
                if project.get("archived") is True:
                    continue
                yield project

    async def _static(self, key: str) -> bool:
        """Browse в схеме прав проекта — только у статичных держателей."""
        try:
            scheme = await self._client.get(
                f"project/{path_segment(key)}/permissionscheme", {"expand": "all"}
            )
        except AdapterError as exc:
            _reraise_unless(exc, _SKIPPABLE)
            # Схему читает администратор проекта; нет — читатели на задачу.
            return False
        grants = [
            p
            for p in scheme.get("permissions") or []
            if isinstance(p, dict)
            and p.get("permission") in {"BROWSE_PROJECTS", BROWSE}
        ]
        if not grants:
            return False
        return all(
            isinstance(g.get("holder"), dict)
            and g["holder"].get("type") in STATIC_HOLDERS
            for g in grants
        )

    async def _project_documents(
        self, project: _Project, wanted: set[str]
    ) -> AsyncIterator[RemoteDocument]:
        async for issue in self._search(project.key):
            issue_id = str(issue.get("id") or "")
            key = str(issue.get("key") or "")
            fields = issue.get("fields")
            if not issue_id or not key or not isinstance(fields, dict):
                continue
            try:
                emails = await self._readers(project, key, fields)
            except AdapterError as exc:
                _reraise_unless(exc, frozenset({"not_found"}))
                # Задачу удалили между поиском и проверкой прав.
                continue
            url = f"{self._client.root}browse/{path_segment(key)}"
            updated = str(fields.get("updated") or "")
            if MODULE_ISSUES in wanted:
                yield RemoteDocument(
                    external_id=f"{ISSUE_PREFIX}{issue_id}",
                    title=f"{key}: {fields.get('summary') or ''}".strip(),
                    url=url,
                    version=updated,
                    kind=RemoteDocumentKind.PAGE,
                    module=MODULE_ISSUES,
                    path=project.name,
                    locator=key,
                    modified_at=parse_datetime(updated),
                    visibility=MaterialVisibility.RESTRICTED,
                    allowed_emails=emails,
                )
            if MODULE_ATTACHMENTS in wanted:
                for attachment in fields.get("attachment") or []:
                    document = self._attachment_document(
                        attachment, f"{project.name}/{key}", url, emails
                    )
                    if document is not None:
                        yield document

    async def _search(self, project_key: str) -> AsyncIterator[dict[str, Any]]:
        jql = f'project = "{project_key}"'
        if self._jql:
            jql += f" AND ({self._jql})"
        jql += " ORDER BY created ASC, key ASC"
        start = 0
        while True:
            try:
                page = await self._client.get(
                    "search",
                    {
                        "jql": jql,
                        "fields": _SEARCH_FIELDS,
                        "startAt": start,
                        "maxResults": SEARCH_LIMIT,
                    },
                )
            except AdapterError as exc:
                if exc.code == "bad_request" and self._jql:
                    # Фильтр админа не разобран Jira: исправит только он.
                    raise AdapterConfigError("jql_invalid") from exc
                raise
            issues = page.get("issues") if isinstance(page, dict) else None
            items = [i for i in issues or [] if isinstance(i, dict)]
            for item in items:
                yield item
            start += len(items)
            total = to_int(page.get("total")) if isinstance(page, dict) else None
            if not items or (total is not None and start >= total):
                return

    def _attachment_document(
        self,
        attachment: Any,
        path: str,
        url: str,
        emails: frozenset[str],
    ) -> RemoteDocument | None:
        if not isinstance(attachment, dict):
            return None
        attachment_id = str(attachment.get("id") or "")
        filename = str(attachment.get("filename") or "")
        if not attachment_id or not filename:
            return None
        if PurePath(filename).suffix.lower() not in supported_extensions():
            note_unsupported(filename, attachment_id)
            return None
        size = to_int(attachment.get("size"))
        if size is not None and size > self._max_bytes:
            note_too_large(attachment_id)
            return None
        content = attachment.get("content")
        created = str(attachment.get("created") or "")
        return RemoteDocument(
            external_id=f"{ATTACHMENT_PREFIX}{attachment_id}",
            title=filename,
            url=url,
            # Вложение Jira не меняется: новая версия файла — новое вложение.
            version=f"{attachment_id}:{size or ''}:{created}",
            kind=RemoteDocumentKind.FILE,
            module=MODULE_ATTACHMENTS,
            path=path,
            locator=content if isinstance(content, str) else "",
            filename=filename,
            size=size,
            modified_at=parse_datetime(created),
            visibility=MaterialVisibility.RESTRICTED,
            allowed_emails=emails,
        )

    # --- права -----------------------------------------------------------------------

    async def _readers(
        self, project: _Project, key: str, fields: Mapping[str, Any]
    ) -> frozenset[str]:
        if project.static and not fields.get("security"):
            if project.readers is None:
                project.readers = await self._browsers(key)
            return project.readers
        return await self._browsers(key)

    async def _browsers(self, issue_key: str) -> frozenset[str]:
        """Почты активных пользователей, которым Jira открывает задачу."""
        emails: set[str] = set()
        start = 0
        for _ in range(MAX_USER_PAGES):
            try:
                page = await self._client.get(
                    "user/permission/search",
                    {
                        "permissions": BROWSE,
                        "issueKey": issue_key,
                        # Пустая строка — все пользователи; «.» в 10.3 отдаёт
                        # не всех (стенд 15.10).
                        "username": "",
                        "startAt": start,
                        "maxResults": USERS_LIMIT,
                    },
                )
            except AdapterError as exc:
                if exc.code == "forbidden":
                    raise _ProjectSkippedError from exc
                raise
            self._answered += 1
            users = (
                [u for u in page if isinstance(u, dict)]
                if isinstance(page, list)
                else []
            )
            if not users:
                return frozenset(emails)
            for user in users:
                if user.get("active") is False:
                    continue
                email = user.get("emailAddress")
                if isinstance(email, str) and "@" in email:
                    emails.add(email.strip().lower())
                else:
                    self._no_email.add(str(user.get("name") or user.get("key")))
            # Конец — пустая страница: в части версий Jira страница режется
            # до проверки прав и бывает короче maxResults посреди списка.
            start += len(users)
        raise AdapterError("too_many_users")

    # --- содержимое ------------------------------------------------------------------

    async def _fetch_issue(self, issue_id: str, *, max_bytes: int) -> FetchedPage:
        issue = await self._client.get(
            f"issue/{path_segment(issue_id)}",
            {"fields": _ISSUE_FIELDS, "expand": "renderedFields"},
        )
        fields = issue.get("fields") if isinstance(issue, dict) else None
        if not isinstance(fields, dict):
            raise AdapterError("issue_missing")
        rendered = issue.get("renderedFields") or {}
        key = str(issue.get("key") or issue_id)
        comment = fields.get("comment") or {}
        raw = [c for c in comment.get("comments") or [] if isinstance(c, dict)]
        comments = visible_comments(
            raw, (rendered.get("comment") or {}).get("comments") or []
        )
        total = to_int(comment.get("total"))
        if total is not None and total > len(raw):
            # Сверх отданных в задаче (до 1000) — постранично.
            comments += visible_comments(
                await self._more_comments(issue_id, len(raw)), []
            )
        description = rendered.get("description")
        html = issue_html(
            key, fields, description if isinstance(description, str) else None, comments
        )
        if len(html.encode("utf-8")) > max_bytes:
            raise AdapterError("document_too_large")
        return FetchedPage(html=html)

    async def _more_comments(
        self, issue_id: str, start: int
    ) -> Sequence[Mapping[str, Any]]:
        found: list[Mapping[str, Any]] = []
        while True:
            page = await self._client.get(
                f"issue/{path_segment(issue_id)}/comment",
                {
                    "startAt": start,
                    "maxResults": COMMENTS_LIMIT,
                    "expand": "renderedBody",
                },
            )
            items = [c for c in page.get("comments") or [] if isinstance(c, dict)]
            found.extend(items)
            start += len(items)
            total = to_int(page.get("total"))
            if not items or (total is not None and start >= total):
                return found


def _project_keys(config: Mapping[str, str]) -> list[str]:
    return [p.strip() for p in config.get("projects", "").split(",") if p.strip()]


def _reraise_unless(exc: AdapterError, codes: frozenset[str]) -> None:
    if exc.retryable or isinstance(exc, AdapterAuthError) or exc.code not in codes:
        raise exc


def build_adapter(
    config: Mapping[str, str],
    credentials: Mapping[str, str],
    http: OutboundClient,
    settings: ConnectorSettings,
    *,
    recorder: Recorder | None = None,
) -> JiraAdapter:
    base_url = config.get("base_url", "").strip()
    if not base_url:
        raise AdapterConfigError("base_url_missing")
    token = credentials.get("token", "").strip()
    if not token:
        raise AdapterAuthError("credentials_missing")
    problem = _config_check(config)
    if problem is not None:
        raise AdapterConfigError(problem)
    return JiraAdapter(
        JiraClient(http, base_url=base_url, token=token, recorder=recorder),
        projects=_project_keys(config),
        jql=config.get("jql", ""),
        max_bytes=settings.download_limit_bytes,
    )


def register(registry: AdapterRegistry, settings: ConnectorSettings) -> None:
    def factory(
        spec: KindSpec,
        config: Mapping[str, str],
        credentials: Mapping[str, str],
        http: OutboundClient,
        options: AdapterOptions,
    ) -> JiraAdapter:
        return build_adapter(
            config, credentials, http, settings, recorder=options.recorder
        )

    registry.register(SPEC, factory)
