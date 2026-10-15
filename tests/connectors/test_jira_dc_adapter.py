"""Адаптер Jira Server/DC (режим organization): каталог, клиент, обход
проектов и задач, права — ответ Jira о читателях задачи (схема прав,
уровень безопасности, автор), вложения, ошибки."""

import pytest
from cryptography.fernet import Fernet

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterConfigError,
    AdapterError,
    FetchedFile,
    FetchedPage,
    RemoteDocument,
)
from corp_ed.connectors.common import counting_skips
from corp_ed.connectors.html import html_to_markdown
from corp_ed.connectors.jira_dc import KIND, SPEC, register
from corp_ed.connectors.jira_dc import adapter as adapter_module
from corp_ed.connectors.jira_dc.adapter import JiraAdapter, build_adapter
from corp_ed.connectors.jira_dc.client import JiraClient, site_root
from corp_ed.connectors.registry import AdapterRegistry
from corp_ed.core.config import ConnectorSettings
from corp_ed.domain.types import ConnectorMode, MaterialVisibility, RemoteDocumentKind
from tests.connectors.fake_jira_dc import (
    ADMIN_TOKEN,
    MEMBER_TOKEN,
    FakeJira,
    sample_jira,
)

KEY = Fernet.generate_key().decode()
SEARCH = "/rest/api/2/search"
MAX_BYTES = 1024 * 1024


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def settings() -> ConnectorSettings:
    return ConnectorSettings(  # type: ignore[arg-type]
        secrets_keys=KEY,
        max_document_bytes=MAX_BYTES,
        max_large_document_bytes=MAX_BYTES,
    )


def make_adapter(
    server: FakeJira,
    token: str = ADMIN_TOKEN,
    *,
    sleep: Sleeps | None = None,
    projects: tuple[str, ...] = (),
    jql: str = "",
) -> JiraAdapter:
    client = JiraClient(
        server.client(), base_url=server.base, token=token, sleep=sleep or Sleeps()
    )
    return JiraAdapter(client, projects=projects, jql=jql, max_bytes=MAX_BYTES)


async def listed(adapter: JiraAdapter, *modules: str) -> dict[str, RemoteDocument]:
    return {d.title: d async for d in adapter.list(list(modules or ("issues",)))}


def emails(*names: str) -> frozenset[str]:
    return frozenset(f"{name}@example.com" for name in names)


@pytest.fixture
def server() -> FakeJira:
    return sample_jira()


# --- каталог и сборка ---------------------------------------------------------------


def test_spec_is_hidden_organization_kind() -> None:
    assert SPEC.kind == KIND == "jira_dc"
    assert SPEC.mode is ConnectorMode.ORGANIZATION and not SPEC.oauth
    assert SPEC.preview and SPEC.base
    assert [m.name for m in SPEC.modules] == ["issues", "attachments"]
    assert [(f.name, f.required) for f in SPEC.config_fields] == [
        ("base_url", True),
        ("projects", False),
        ("jql", False),
    ]
    assert [(f.name, f.secret) for f in SPEC.credential_fields] == [("token", True)]
    assert SPEC.url_field == "base_url"


def test_preview_kind_is_offered_only_when_enabled(server: FakeJira) -> None:
    registry = AdapterRegistry()
    register(registry, settings())
    assert not registry.offered(KIND)
    adapter = registry.build(
        KIND, {"base_url": server.base}, {"token": ADMIN_TOKEN}, server.client()
    )
    assert isinstance(adapter, JiraAdapter)
    enabled = AdapterRegistry(enabled_preview_kinds=frozenset({KIND}))
    register(enabled, settings())
    assert [s.kind for s in enabled.kinds()] == [KIND]


@pytest.mark.parametrize(
    ("config", "code"),
    [
        ({"projects": "HR, eng"}, "projects_invalid"),
        ({"projects": 'HR" OR project = "X'}, "projects_invalid"),
        ({"jql": "updated >= -30d ORDER BY key"}, "jql_order_by"),
    ],
)
def test_config_check_rejects_bad_projects_and_order_by(
    config: dict[str, str], code: str
) -> None:
    assert SPEC.config_check is not None
    assert SPEC.config_check(config) == code
    assert (
        SPEC.config_check({"projects": "HR,ENG_2", "jql": "updated >= -365d"}) is None
    )
    with pytest.raises(AdapterConfigError, match=code):
        build_adapter(
            {"base_url": "https://jira.example.com", **config},
            {"token": ADMIN_TOKEN},
            sample_jira().client(),
            settings(),
        )


def test_build_requires_address_and_token(server: FakeJira) -> None:
    with pytest.raises(AdapterConfigError, match="base_url_missing"):
        build_adapter({}, {"token": ADMIN_TOKEN}, server.client(), settings())
    with pytest.raises(AdapterAuthError, match="credentials_missing"):
        build_adapter({"base_url": server.base}, {}, server.client(), settings())


@pytest.mark.parametrize(
    ("address", "root"),
    [
        ("https://jira.company.ru", "https://jira.company.ru/"),
        ("https://jira.company.ru/jira/", "https://jira.company.ru/jira/"),
        ("https://jira.company.ru/rest/api/2/", "https://jira.company.ru/"),
    ],
)
def test_site_root(address: str, root: str) -> None:
    assert site_root(address) == root


# --- клиент и проверка ------------------------------------------------------------


async def test_check_accepts_token_and_rejects_wrong_one(server: FakeJira) -> None:
    await make_adapter(server).check()
    with pytest.raises(AdapterAuthError, match="unauthorized"):
        await make_adapter(server, "wrong").check()
    assert server.paths()[0] == "/rest/api/2/myself"


async def test_inactive_owner_is_refused(server: FakeJira) -> None:
    server.users["admin"]["active"] = False
    with pytest.raises(AdapterAuthError, match="user_inactive"):
        await make_adapter(server).check()


async def test_rate_limit_and_unavailable_node_wait(server: FakeJira) -> None:
    server.rate_limit_hits = 2
    server.server_errors = 1
    sleeps = Sleeps()
    await make_adapter(server, sleep=sleeps).check()
    # 503 без Retry-After — следующая ступень общей лестницы ожидания.
    assert sleeps.calls == [2.0, 2.0, 4.0]


async def test_rate_limit_is_bounded_and_retryable(server: FakeJira) -> None:
    server.rate_limit_hits = 100
    server.retry_after = "600"
    sleeps = Sleeps()
    with pytest.raises(AdapterError) as caught:
        await make_adapter(server, sleep=sleeps).check()
    assert caught.value.code == "rate_limited" and caught.value.retryable
    assert sleeps.calls == [30.0] * 4


async def test_server_errors_end_retryable(server: FakeJira) -> None:
    server.server_errors = 100
    with pytest.raises(AdapterError) as caught:
        await make_adapter(server).check()
    assert caught.value.code == "http_503" and caught.value.retryable


async def test_login_page_instead_of_json(server: FakeJira) -> None:
    server.login_page = True
    with pytest.raises(AdapterError, match="not_json"):
        await make_adapter(server).check()


# --- задачи и права ---------------------------------------------------------------


async def test_readers_are_what_jira_reports_for_each_issue(server: FakeJira) -> None:
    documents = await listed(make_adapter(server))

    assert set(documents) == {
        "HR-1: Отпуск",
        "HR-2: Справка для Дэйва",
        "ENG-1: Архитектура",
        "ENG-2: Инцидент",
        "OPEN-1: Регламент",
    }
    readers = {title: d.allowed_emails for title, d in documents.items()}
    # Группа hr без деактивированной erin и без пользователя без почты.
    assert readers["HR-1: Отпуск"] == emails("admin", "alice", "carol")
    # «Автор задачи» в схеме прав — dave видит свою задачу.
    assert readers["HR-2: Справка для Дэйва"] == emails(
        "admin", "alice", "carol", "dave"
    )
    # Роль проекта: группа engineering и dave.
    assert readers["ENG-1: Архитектура"] == emails("admin", "bob", "carol", "dave")
    # Уровень безопасности сужает роль до своих участников.
    assert readers["ENG-2: Инцидент"] == emails("admin", "carol")
    # «Любой» — все активные пользователи с почтой, а не вся компания.
    assert readers["OPEN-1: Регламент"] == emails(
        "admin", "alice", "bob", "carol", "dave"
    )
    for document in documents.values():
        assert document.visibility is MaterialVisibility.RESTRICTED
        assert document.kind is RemoteDocumentKind.PAGE
        assert document.module == "issues"
    hr = documents["HR-1: Отпуск"]
    assert hr.external_id.startswith("issue:")
    assert hr.url == f"{server.base}browse/HR-1"
    assert hr.path == "Кадры"
    assert hr.version == "2026-10-01T10:00:00.000+0000"
    assert hr.modified_at is not None and hr.modified_at.year == 2026


async def test_readers_are_cached_only_where_the_issue_does_not_matter(
    server: FakeJira,
) -> None:
    server.add_issue("OPEN", "Отпуск в декабре", "<p>Да.</p>")
    server.add_issue("HR", "Третья", "<p>Да.</p>")

    await listed(make_adapter(server))

    asked = [
        q["issueKey"] for p, q in server.calls if p.endswith("user/permission/search")
    ]
    # OPEN: «любой» не зависит от задачи — один ответ на проект (и пустая
    # страница в конце). HR: «автор задачи» — на каждую задачу. ENG:
    # роль — одна на проект, задача с уровнем безопасности — отдельно.
    assert asked.count("OPEN-1") == 2 and "OPEN-2" not in asked
    assert {"HR-1", "HR-2", "HR-3"} <= set(asked)
    assert asked.count("ENG-1") == 2 and asked.count("ENG-2") == 2


async def test_unreadable_scheme_means_asking_per_issue(server: FakeJira) -> None:
    server.add_issue("OPEN", "Ещё", "<p>Да.</p>")
    server.hidden_schemes.add("OPEN")

    documents = await listed(make_adapter(server))

    asked = [
        q["issueKey"] for p, q in server.calls if p.endswith("user/permission/search")
    ]
    # Схемы не видно — неизвестно, зависят ли права от задачи: спрашиваем
    # о каждой. Дороже, но верно.
    assert "OPEN-1" in asked and "OPEN-2" in asked
    assert documents["OPEN-2: Ещё"].allowed_emails == emails(
        "admin", "alice", "bob", "carol", "dave"
    )


async def test_project_without_admin_rights_is_skipped(server: FakeJira) -> None:
    server.admins.discard("admin")
    server.projects["OPEN"]["project_admins"].add("admin")

    documents = await listed(make_adapter(server))

    # Читателей задач Jira сказала только по OPEN: остальные проекты не
    # индексируются вовсе, а не «всем» и не «никому».
    assert set(documents) == {"OPEN-1: Регламент"}


async def test_token_without_admin_rights_anywhere_stops_the_connector(
    server: FakeJira,
) -> None:
    with pytest.raises(AdapterConfigError, match="admin_required"):
        await listed(make_adapter(server, MEMBER_TOKEN))


async def test_users_without_email_and_inactive_get_nothing(server: FakeJira) -> None:
    documents = await listed(make_adapter(server))
    everyone = frozenset().union(*(d.allowed_emails for d in documents.values()))
    assert "erin@example.com" not in everyone
    assert all("nomail" not in email for email in everyone)


async def test_permission_search_is_paged_until_empty(
    server: FakeJira, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adapter_module, "USERS_LIMIT", 2)
    server.users_cap = 2

    documents = await listed(make_adapter(server, projects=("OPEN",)))

    assert documents["OPEN-1: Регламент"].allowed_emails == emails(
        "admin", "alice", "bob", "carol", "dave"
    )
    starts = [
        q["startAt"] for p, q in server.calls if p.endswith("user/permission/search")
    ]
    # Шесть активных читателей (nomail — без почты, но Jira его отдаёт).
    assert starts == ["0", "2", "4", "6"]


async def test_search_is_paged_even_when_jira_caps_the_page(server: FakeJira) -> None:
    for n in range(7):
        server.add_issue("OPEN", f"Задача {n}", "<p>x</p>")
    server.search_cap = 3

    documents = await listed(make_adapter(server, projects=("OPEN",)))

    assert len(documents) == 8
    searches = [q for p, q in server.calls if p == SEARCH]
    assert [q["startAt"] for q in searches] == ["0", "3", "6"]
    assert searches[0]["jql"] == 'project = "OPEN" ORDER BY created ASC, key ASC'


async def test_configured_projects_and_jql(server: FakeJira) -> None:
    documents = await listed(
        make_adapter(server, projects=("ENG", "GONE"), jql="updated >= -365d")
    )

    assert set(documents) == {"ENG-1: Архитектура", "ENG-2: Инцидент"}
    searches = [q["jql"] for p, q in server.calls if p == SEARCH]
    assert searches == [
        'project = "ENG" AND (updated >= -365d) ORDER BY created ASC, key ASC'
    ]


async def test_invalid_jql_is_a_configuration_error(server: FakeJira) -> None:
    with pytest.raises(AdapterConfigError, match="jql_invalid"):
        await listed(make_adapter(server, jql="status = invalid"))


async def test_deleted_issue_between_search_and_rights_is_skipped(
    server: FakeJira,
) -> None:
    server.vanished.add("HR-2")
    documents = await listed(make_adapter(server, projects=("HR",)))
    assert set(documents) == {"HR-1: Отпуск"}


async def test_rights_change_without_new_version(server: FakeJira) -> None:
    before = await listed(make_adapter(server))
    server.groups["engineering"].discard("bob")
    after = await listed(make_adapter(server))

    title = "ENG-1: Архитектура"
    assert after[title].version == before[title].version
    assert "bob@example.com" in before[title].allowed_emails
    assert "bob@example.com" not in after[title].allowed_emails


async def test_new_comment_changes_version(server: FakeJira) -> None:
    before = await listed(make_adapter(server))
    server.add_comment("OPEN-1", "bob", "<p>Новый комментарий.</p>")
    server.issue_by_key("OPEN-1")["updated"] = "2026-10-05T09:00:00.000+0000"
    after = await listed(make_adapter(server))
    title = "OPEN-1: Регламент"
    assert after[title].version != before[title].version


# --- содержимое ---------------------------------------------------------------------


async def test_issue_page_has_description_and_visible_comments(
    server: FakeJira,
) -> None:
    adapter = make_adapter(server)
    document = (await listed(adapter))["HR-1: Отпуск"]

    content = await adapter.fetch(document, max_bytes=MAX_BYTES)

    assert isinstance(content, FetchedPage)
    markdown = html_to_markdown(content.html)
    assert markdown.startswith("# HR-1: Отпуск")
    assert "Отпуск — **28** дней." in markdown
    assert "Заявление — за две недели." in markdown
    assert "Alice, 2026-09-02" in markdown
    # Комментарий только для роли — не для всех читателей задачи.
    assert "Бюджет" not in markdown
    assert "Статус: To Do" in markdown


async def test_summary_is_escaped(server: FakeJira) -> None:
    key = server.add_issue("OPEN", "<script>alert(1)</script> & Ко", "")
    adapter = make_adapter(server)
    document = (await listed(adapter))[f"{key}: <script>alert(1)</script> & Ко"]
    content = await adapter.fetch(document, max_bytes=MAX_BYTES)
    assert isinstance(content, FetchedPage)
    assert "<script>" not in content.html
    assert "&lt;script&gt;" in content.html


async def test_comments_beyond_the_issue_are_paged(server: FakeJira) -> None:
    for n in range(5):
        server.add_comment("OPEN-1", "bob", f"<p>Комментарий {n}.</p>")
    server.inline_comments = 2
    adapter = make_adapter(server)
    document = (await listed(adapter))["OPEN-1: Регламент"]

    content = await adapter.fetch(document, max_bytes=MAX_BYTES)

    assert isinstance(content, FetchedPage)
    for n in range(5):
        assert f"Комментарий {n}." in content.html
    assert any(p.endswith("/comment") for p in server.paths())


async def test_issue_too_large(server: FakeJira) -> None:
    server.issue_by_key("OPEN-1")["description"] = "<p>" + "я" * 5000 + "</p>"
    adapter = make_adapter(server)
    document = (await listed(adapter))["OPEN-1: Регламент"]
    with pytest.raises(AdapterError, match="document_too_large"):
        await adapter.fetch(document, max_bytes=1000)


async def test_issue_gone_before_fetch(server: FakeJira) -> None:
    adapter = make_adapter(server)
    document = (await listed(adapter))["OPEN-1: Регламент"]
    server.issues.pop(server.issue_by_key("OPEN-1")["id"])
    with pytest.raises(AdapterError, match="not_found"):
        await adapter.fetch(document, max_bytes=MAX_BYTES)


# --- вложения -----------------------------------------------------------------------


async def test_attachments_inherit_issue_readers(server: FakeJira) -> None:
    adapter = make_adapter(server)
    with counting_skips() as tally:
        documents = await listed(adapter, "issues", "attachments")

    attachment = documents["Заявление.md"]
    assert attachment.module == "attachments"
    assert attachment.kind is RemoteDocumentKind.FILE
    assert attachment.allowed_emails == documents["HR-1: Отпуск"].allowed_emails
    assert attachment.path == "Кадры/HR-1"
    assert attachment.url == f"{server.base}browse/HR-1"
    assert attachment.external_id.startswith("att:")
    # .exe ассистент не читает — посчитан как пропущенный.
    assert "схема.exe" not in documents
    assert tally.formats() == {".exe": 1}

    content = await adapter.fetch(attachment, max_bytes=MAX_BYTES)
    assert isinstance(content, FetchedFile)
    assert content.filename == "Заявление.md"
    assert content.data.startswith(b"# \xd0\x97")


async def test_only_attachments_module(server: FakeJira) -> None:
    documents = await listed(make_adapter(server), "attachments")
    assert set(documents) == {"Заявление.md"}


async def test_too_large_attachment_is_counted_not_listed(server: FakeJira) -> None:
    server.add_attachment("OPEN-1", "big.pdf", b"%PDF", size=MAX_BYTES + 1)
    with counting_skips() as tally:
        documents = await listed(make_adapter(server), "attachments")
    assert "big.pdf" not in documents
    assert len(tally.too_large) == 1


async def test_attachment_from_foreign_host_is_not_downloaded(
    server: FakeJira,
) -> None:
    server.foreign_content = True
    adapter = make_adapter(server)
    attachment = (await listed(adapter, "attachments"))["Заявление.md"]
    with pytest.raises(AdapterError, match="link_foreign"):
        await adapter.fetch(attachment, max_bytes=MAX_BYTES)
    assert not any("evil" in p for p in server.paths())


async def test_attachment_download_needs_the_token(server: FakeJira) -> None:
    adapter = make_adapter(server)
    attachment = (await listed(adapter, "attachments"))["Заявление.md"]
    server.tokens.clear()
    # Без токена Jira уводит на login.jsp: страница входа — не файл.
    with pytest.raises(AdapterError):
        await adapter.fetch(attachment, max_bytes=MAX_BYTES)


async def test_context_path_installation() -> None:
    server = sample_jira()
    server.context = "/jira"
    adapter = make_adapter(server)
    documents = await listed(adapter, "issues", "attachments")
    assert documents["HR-1: Отпуск"].url == "https://jira.example.com/jira/browse/HR-1"
    content = await adapter.fetch(documents["Заявление.md"], max_bytes=MAX_BYTES)
    assert isinstance(content, FetchedFile)


async def test_unknown_document(server: FakeJira) -> None:
    document = RemoteDocument(
        external_id="x:1",
        title="x",
        url="",
        version="",
        kind=RemoteDocumentKind.PAGE,
        module="issues",
    )
    with pytest.raises(AdapterError, match="unknown_document"):
        await make_adapter(server).fetch(document, max_bytes=MAX_BYTES)


async def test_unknown_modules_list_nothing(server: FakeJira) -> None:
    assert await listed(make_adapter(server), "wiki") == {}
    assert server.calls == []
