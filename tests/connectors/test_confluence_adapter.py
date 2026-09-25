"""Адаптер Confluence Server/DC: клиент, обход пространств, наследование
ограничений чтения, группы, вложения, storage-формат → HTML."""

from typing import Any

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
from corp_ed.connectors.confluence import KIND, SPEC
from corp_ed.connectors.confluence.adapter import ConfluenceAdapter, build_adapter
from corp_ed.connectors.confluence.client import BasicAuth, ConfluenceClient, TokenAuth
from corp_ed.connectors.confluence.storage import storage_to_html
from corp_ed.connectors.html import html_to_markdown
from corp_ed.connectors.registry import default_registry
from corp_ed.core.config import ConnectorSettings
from corp_ed.domain.types import ConnectorMode, MaterialVisibility, RemoteDocumentKind
from tests.connectors.fake_confluence import (
    PAT,
    SERVICE_PASSWORD,
    SERVICE_USER,
    FakeConfluence,
    sample_confluence,
)

KEY = Fernet.generate_key().decode()
MAX_BYTES = 1024 * 1024


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def settings() -> ConnectorSettings:
    return ConnectorSettings(secrets_keys=KEY, max_document_bytes=MAX_BYTES)  # type: ignore[arg-type]


def make_adapter(server: FakeConfluence, **config: str) -> ConfluenceAdapter:
    full = {"base_url": server.base, **config}
    return build_adapter(full, {"token": PAT}, server.client(), settings())


async def listed(
    adapter: ConfluenceAdapter, *modules: str
) -> dict[str, RemoteDocument]:
    return {d.external_id: d async for d in adapter.list(list(modules))}


@pytest.fixture
def server() -> FakeConfluence:
    return sample_confluence()


# --- каталог и сборка ---------------------------------------------------------


def test_spec_is_organization_mode() -> None:
    assert SPEC.kind == KIND == "confluence"
    assert SPEC.mode is ConnectorMode.ORGANIZATION and not SPEC.oauth
    assert [m.name for m in SPEC.modules] == ["pages", "attachments"]
    assert [f.name for f in SPEC.config_fields] == [
        "base_url",
        "spaces",
        "email_template",
    ]
    assert [f.name for f in SPEC.credential_fields] == ["token", "username", "password"]
    assert {f.name: f.secret for f in SPEC.credential_fields} == {
        "token": True,
        "username": False,
        "password": True,
    }
    assert SPEC.url_field == "base_url"


def test_registry_builds_confluence(server: FakeConfluence) -> None:
    registry = default_registry(settings())
    adapter = registry.build(
        "confluence", {"base_url": server.base}, {"token": PAT}, server.client()
    )
    assert isinstance(adapter, ConfluenceAdapter)
    basic = registry.build(
        "confluence",
        {"base_url": server.base},
        {"username": SERVICE_USER, "password": SERVICE_PASSWORD},
        server.client(),
    )
    assert isinstance(basic, ConfluenceAdapter)


def test_build_validates_credentials_and_template(server: FakeConfluence) -> None:
    with pytest.raises(AdapterAuthError, match="credentials_missing"):
        build_adapter(
            {"base_url": server.base}, {"username": "x"}, server.client(), settings()
        )
    with pytest.raises(AdapterConfigError, match="base_url_missing"):
        build_adapter({}, {"token": PAT}, server.client(), settings())
    with pytest.raises(AdapterConfigError, match="email_template_invalid"):
        build_adapter(
            {"base_url": server.base, "email_template": "nobody@x"},
            {"token": PAT},
            server.client(),
            settings(),
        )


# --- клиент ---------------------------------------------------------------------


async def test_check_with_token_and_basic(server: FakeConfluence) -> None:
    await make_adapter(server).check()
    basic = ConfluenceAdapter(
        ConfluenceClient(
            server.client(),
            base_url=server.base,
            auth=BasicAuth(SERVICE_USER, SERVICE_PASSWORD),
        ),
        spaces=[],
        email_template="{username}",
        max_bytes=MAX_BYTES,
    )
    await basic.check()
    assert server.auth_log == ["token", "basic"]


async def test_check_rejects_anonymous_and_bad_credentials(
    server: FakeConfluence,
) -> None:
    bad = ConfluenceAdapter(
        ConfluenceClient(
            server.client(), base_url=server.base, auth=TokenAuth("wrong")
        ),
        spaces=[],
        email_template="{username}",
        max_bytes=MAX_BYTES,
    )
    with pytest.raises(AdapterAuthError, match="anonymous"):
        await bad.check()
    with pytest.raises(AdapterAuthError, match="unauthorized"):
        await listed(bad, "pages")


async def test_login_page_instead_of_json(server: FakeConfluence) -> None:
    server.login_page = True
    with pytest.raises(AdapterError, match="not_json"):
        await make_adapter(server).check()


async def test_rate_limit_waits_retry_after_then_gives_up(
    server: FakeConfluence,
) -> None:
    sleeps = Sleeps()
    client = ConfluenceClient(
        server.client(), base_url=server.base, auth=TokenAuth(PAT), sleep=sleeps
    )
    server.rate_limit_hits = 2
    server.retry_after = "3"
    assert (await client.get("user/current"))["username"] == SERVICE_USER
    assert sleeps.calls == [3.0, 3.0]
    server.rate_limit_hits = 10
    server.retry_after = None
    with pytest.raises(AdapterError) as excinfo:
        await client.get("user/current")
    assert excinfo.value.code == "rate_limited" and excinfo.value.retryable
    assert sleeps.calls[2:] == [1.0, 2.0, 4.0]


async def test_server_error_is_retryable(server: FakeConfluence) -> None:
    server.server_errors = 1
    with pytest.raises(AdapterError) as excinfo:
        await make_adapter(server).check()
    assert excinfo.value.code == "http_502" and excinfo.value.retryable


async def test_pagination_follows_start_limit(server: FakeConfluence) -> None:
    server.page_limit_cap = 2
    documents = await listed(make_adapter(server, spaces="HR"), "pages")
    assert set(documents) == {"page:100", "page:101", "page:102", "page:103"}
    starts = [q.get("start") for path, q in server.calls if path == "content"]
    assert starts == ["0", "2"]


async def test_links_must_stay_on_the_confluence_host(server: FakeConfluence) -> None:
    client = ConfluenceClient(
        server.client(), base_url=server.base, auth=TokenAuth(PAT)
    )
    assert client.absolute("/display/HR/X") == f"{server.base}display/HR/X"
    with pytest.raises(AdapterError, match="link_foreign"):
        client.absolute("https://evil.example.com/x")


# --- обход и права ------------------------------------------------------------------


async def test_walk_lists_global_spaces_with_inherited_readers(
    server: FakeConfluence,
) -> None:
    documents = await listed(make_adapter(server), "pages")
    assert set(documents) == {
        "page:100",
        "page:101",
        "page:102",
        "page:103",
        "page:200",
    }
    vacation = documents["page:100"]
    assert vacation.kind is RemoteDocumentKind.PAGE
    assert vacation.visibility is MaterialVisibility.TENANT
    assert vacation.allowed_emails == frozenset()
    assert vacation.path == "Кадры"
    assert vacation.version == "1:2026-03-01T10:00:00.000+03:00"
    assert vacation.url == f"{server.base}display/HR/Отпуск"
    assert vacation.modified_at is not None and vacation.modified_at.year == 2026
    salaries = documents["page:101"]
    assert salaries.visibility is MaterialVisibility.RESTRICTED
    # Пользователь anna + группа hr-team (anna, boris).
    assert salaries.allowed_emails == frozenset({"anna", "boris"})
    assert salaries.path == "Кадры/Отпуск"
    assert salaries.version.startswith("3:")
    bonuses = documents["page:102"]
    # Своё ограничение {boris, ceo} ∩ родительское {anna, boris} = {boris}.
    assert bonuses.allowed_emails == frozenset({"boris"})
    assert bonuses.path == "Кадры/Отпуск/Зарплаты"
    strategy = documents["page:200"]
    assert strategy.allowed_emails == frozenset({"ceo"})
    assert "page:300" not in documents  # личное пространство не глобальное


async def test_email_template_and_space_filter(server: FakeConfluence) -> None:
    documents = await listed(
        make_adapter(
            server, spaces=" BOARD , NOPE", email_template="{username}@corp.ru"
        ),
        "pages",
    )
    assert set(documents) == {"page:200"}
    assert documents["page:200"].allowed_emails == frozenset({"ceo@corp.ru"})
    # Несуществующее пространство пропущено, а не уронило обход.
    assert ("space/NOPE", {}) in server.calls


async def test_restrictions_are_cached_per_run(server: FakeConfluence) -> None:
    await listed(make_adapter(server, spaces="HR"), "pages")
    restriction_calls = [
        p for p, _ in server.calls if p.endswith("restriction/byOperation")
    ]
    assert sorted(restriction_calls) == sorted(
        f"content/{i}/restriction/byOperation" for i in ("100", "101", "102", "103")
    )
    group_calls = [p for p, _ in server.calls if p.startswith("group/")]
    assert group_calls == ["group/hr-team/member"]


async def test_unreadable_group_grants_nobody(server: FakeConfluence) -> None:
    server.unreadable_groups.add("hr-team")
    documents = await listed(make_adapter(server, spaces="HR"), "pages")
    assert documents["page:101"].allowed_emails == frozenset({"anna"})


async def test_hidden_page_is_skipped_not_fatal(server: FakeConfluence) -> None:
    server.hidden_pages.add("101")
    documents = await listed(make_adapter(server, spaces="HR"), "pages")
    assert "page:101" not in documents
    # Потомок ссылается на предка, который не виден: ограничение предка
    # прочитать нельзя → страница пропущена, обход продолжился.
    assert "page:102" not in documents
    assert "page:100" in documents and "page:103" in documents


async def test_attachments_inherit_page_readers(server: FakeConfluence) -> None:
    documents = await listed(make_adapter(server, spaces="HR"), "attachments")
    assert set(documents) == {"att:500", "att:503"}  # png и большой pdf отсеяны
    rules = documents["att:500"]
    assert rules.kind is RemoteDocumentKind.FILE
    assert rules.filename == "Правила.docx"
    assert rules.visibility is MaterialVisibility.TENANT
    assert rules.path == "Кадры/Отпуск"
    assert rules.version == "1:2026-03-02T12:00:00.000+03:00"
    assert rules.url.startswith(f"{server.base}pages/viewpageattachments.action")
    sheet = documents["att:503"]
    assert sheet.visibility is MaterialVisibility.RESTRICTED
    assert sheet.allowed_emails == frozenset({"anna", "boris"})
    assert "page:100" not in documents  # модуль страниц не выбран


# --- fetch --------------------------------------------------------------------------


async def test_fetch_page_converts_storage_format(server: FakeConfluence) -> None:
    adapter = make_adapter(server, spaces="HR")
    documents = await listed(adapter, "pages")
    content = await adapter.fetch(documents["page:100"], max_bytes=MAX_BYTES)
    assert isinstance(content, FetchedPage)
    markdown = html_to_markdown(content.html)
    assert markdown.startswith("# Отпуск")
    assert "28 календарных дней" in markdown
    assert "Заявление за две недели" in markdown
    assert "toc" not in markdown


async def test_fetch_empty_page_is_error(server: FakeConfluence) -> None:
    adapter = make_adapter(server, spaces="HR")
    documents = await listed(adapter, "pages")
    with pytest.raises(AdapterError, match="empty_page"):
        await adapter.fetch(documents["page:103"], max_bytes=MAX_BYTES)


async def test_fetch_attachment_downloads_from_confluence(
    server: FakeConfluence,
) -> None:
    adapter = make_adapter(server, spaces="HR")
    documents = await listed(adapter, "attachments")
    content = await adapter.fetch(documents["att:503"], max_bytes=MAX_BYTES)
    assert isinstance(content, FetchedFile)
    assert content.filename == "Ведомость.txt"
    assert content.data == "секретная ведомость".encode()


async def test_fetch_attachment_too_large_by_metadata(server: FakeConfluence) -> None:
    adapter = make_adapter(server, spaces="HR")
    document = RemoteDocument(
        external_id="att:502",
        title="Большой.pdf",
        url="",
        version="1",
        kind=RemoteDocumentKind.FILE,
        module="attachments",
    )
    with pytest.raises(AdapterError, match="document_too_large"):
        await adapter.fetch(document, max_bytes=MAX_BYTES)


async def test_fetch_attachment_login_page_is_failure(server: FakeConfluence) -> None:
    server.download_content_type = "text/html"
    adapter = make_adapter(server, spaces="HR")
    documents = await listed(adapter, "attachments")
    with pytest.raises(AdapterError, match="download_failed"):
        await adapter.fetch(documents["att:500"], max_bytes=MAX_BYTES)


async def test_fetch_unknown_document(server: FakeConfluence) -> None:
    document = RemoteDocument(
        external_id="x:1",
        title="",
        url="",
        version="",
        kind=RemoteDocumentKind.PAGE,
        module="pages",
    )
    with pytest.raises(AdapterError, match="unknown_document"):
        await make_adapter(server).fetch(document, max_bytes=MAX_BYTES)


# --- storage-формат -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("storage", "expected", "absent"),
    [
        (
            '<ac:structured-macro ac:name="code">'
            '<ac:parameter ac:name="language">python</ac:parameter>'
            "<ac:plain-text-body><![CDATA[print(1)\nprint(2)]]></ac:plain-text-body></ac:structured-macro>",
            "print(1)",
            "language",
        ),
        (
            '<ac:structured-macro ac:name="warning"><ac:rich-text-body>'
            "<p>Осторожно</p></ac:rich-text-body></ac:structured-macro>",
            "Осторожно",
            "ac:",
        ),
        (
            '<p>См. <ac:link><ri:page ri:content-title="Регламент" /></ac:link> и '
            '<ac:link><ri:attachment ri:filename="форма.docx" /></ac:link></p>',
            "Регламент",
            "ri:",
        ),
        (
            "<ac:task-list><ac:task><ac:task-id>1</ac:task-id><ac:task-status>incomplete</ac:task-status>"
            "<ac:task-body>Подписать договор</ac:task-body></ac:task></ac:task-list>",
            "Подписать договор",
            "incomplete",
        ),
        (
            '<ac:layout><ac:layout-section ac:type="two_equal">'
            "<ac:layout-cell><p>Слева</p></ac:layout-cell>"
            "<ac:layout-cell><p>Справа</p></ac:layout-cell></ac:layout-section></ac:layout>",
            "Справа",
            "layout",
        ),
        (
            '<ac:structured-macro ac:name="jira"><ac:parameter ac:name="key">'
            "ABC-1</ac:parameter></ac:structured-macro><p>Текст</p>",
            "Текст",
            "ABC-1",
        ),
        (
            '<p>Фото: <ac:image><ri:attachment ri:filename="a.png" /></ac:image>'
            " подпись</p>",
            "подпись",
            "a.png",
        ),
    ],
)
def test_storage_macros_become_text(storage: str, expected: str, absent: str) -> None:
    markdown = html_to_markdown(storage_to_html("Страница", storage))
    assert markdown.startswith("# Страница")
    assert expected in markdown
    assert absent not in markdown


def test_storage_unknown_macro_keeps_its_text() -> None:
    html = storage_to_html(
        "T",
        '<ac:structured-macro ac:name="mystery"><ac:rich-text-body>'
        "<p>Внутри</p></ac:rich-text-body></ac:structured-macro>",
    )
    assert "Внутри" in html_to_markdown(html)


def test_storage_title_is_escaped() -> None:
    assert storage_to_html("<b>x</b>", "<p>y</p>").startswith(
        "<h1>&lt;b&gt;x&lt;/b&gt;</h1>"
    )


def _unused(_: Any) -> None:  # pragma: no cover - для mypy в тестах не нужен
    pass
