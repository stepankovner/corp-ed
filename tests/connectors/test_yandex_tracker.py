"""Модуль Трекера вида yandex360: задачи очередей, доступных сотруднику,
комментарии и вложения; заголовок организации; ошибки API Трекера."""

import pytest
from cryptography.fernet import Fernet

from corp_ed.connectors.base import (
    AdapterConfigError,
    AdapterError,
    FetchedFile,
    FetchedMarkdown,
    RemoteDocument,
)
from corp_ed.connectors.common import counting_skips
from corp_ed.connectors.registry import default_registry
from corp_ed.connectors.yandex import SPEC
from corp_ed.connectors.yandex import tracker as tracker_module
from corp_ed.connectors.yandex.adapter import YandexAdapter, build_adapter
from corp_ed.core.config import ConnectorSettings
from corp_ed.domain.types import MaterialVisibility, RemoteDocumentKind
from tests.connectors.fake_yandex import (
    ACCESS_TOKEN,
    CLIENT_ID,
    CLIENT_SECRET,
    CLOUD_ORG_ID,
    DISK_API,
    OAUTH_SERVER,
    ORG_ID,
    REFRESH_TOKEN,
    TRACKER_API,
    TRACKER_HOST,
    WIKI_API,
    FakeYandex,
    sample_tracker,
)

KEY = Fernet.generate_key().decode()
MAX_BYTES = 1024


def settings(**overrides: str) -> ConnectorSettings:
    return ConnectorSettings(
        secrets_keys=KEY,
        yandex_oauth_server=OAUTH_SERVER,
        yandex_disk_api=DISK_API,
        yandex_wiki_api=WIKI_API,
        yandex_tracker_api=TRACKER_API,
        max_document_bytes=MAX_BYTES,
        max_large_document_bytes=MAX_BYTES,
        **overrides,
    )  # type: ignore[arg-type]


def make_adapter(server: FakeYandex, **config: str) -> YandexAdapter:
    return build_adapter(
        {"client_id": CLIENT_ID, "org_id": ORG_ID, **config},
        {
            "client_secret": CLIENT_SECRET,
            "access_token": ACCESS_TOKEN,
            "refresh_token": REFRESH_TOKEN,
            "expires_at": "0",
        },
        server.client(),
        settings(),
    )


async def listed(adapter: YandexAdapter) -> dict[str, RemoteDocument]:
    return {d.external_id: d async for d in adapter.list(["tracker"])}


@pytest.fixture(autouse=True)
def no_wait(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(tracker_module, "_sleep", sleep)
    return waits


@pytest.fixture
def server() -> FakeYandex:
    return sample_tracker()


# --- каталог ---------------------------------------------------------------------


def test_tracker_is_a_preview_module_with_its_fields() -> None:
    tracker = next(m for m in SPEC.modules if m.name == "tracker")
    assert tracker.preview
    assert "Трекер" in tracker.title
    names = [f.name for f in SPEC.config_fields]
    assert names[-2:] == ["tracker_queues", "tracker_cloud_org_id"]
    assert all(not f.required for f in SPEC.config_fields[1:])
    # Право Трекера — необязательное: у уже выданных токенов его нет.
    assert "tracker" not in SPEC.extra["app_scopes"]
    assert "tracker:read" in SPEC.extra["app_scopes_optional"]


@pytest.mark.parametrize(
    ("config", "code"),
    [
        ({"tracker_queues": "SUP, hr\nDEV2"}, None),
        ({"tracker_queues": "SUP;DROP"}, "tracker_queues_invalid"),
        ({"tracker_queues": "1SUP"}, "tracker_queues_invalid"),
        ({"tracker_cloud_org_id": CLOUD_ORG_ID}, None),
        ({"tracker_cloud_org_id": "bpf/../x"}, "tracker_cloud_org_id_invalid"),
    ],
)
def test_config_check(config: dict[str, str], code: str | None) -> None:
    assert SPEC.config_check is not None
    assert SPEC.config_check({"client_id": "x", **config}) == code


def test_module_hidden_until_enabled() -> None:
    hidden = default_registry(settings()).spec("yandex360")
    assert "tracker" not in hidden.module_names
    enabled = default_registry(settings(preview_modules="tracker")).spec("yandex360")
    assert {"disk", "shared_disks", "wiki", "tracker"} == enabled.module_names


# --- обход -----------------------------------------------------------------------


async def test_lists_issues_of_queues_visible_to_the_employee(
    server: FakeYandex,
) -> None:
    with counting_skips() as skips:
        documents = await listed(make_adapter(server))

    # Скрытая задача и закрытая очередь (403) не попадают; обход не падает.
    assert set(documents) == {
        "ytracker:id-1",
        "ytracker:id-2",
        "ytracker:id-4",
        "ytracker-file:id-1:a1",
    }
    issue = documents["ytracker:id-1"]
    assert issue.kind is RemoteDocumentKind.PAGE and issue.module == "tracker"
    assert issue.title == "SUP-1: Клиент Альфа: сроки доставки"
    assert issue.url == "https://tracker.yandex.ru/SUP-1"
    assert issue.path == "Трекер / Поддержка"
    assert issue.locator == "id-1"
    assert issue.version == "1:2026-09-01T10:00:00.000+0000:3:0"
    # Права — по листингу сотрудника (per_user): ядро сочтёт ограниченным.
    assert issue.visibility is MaterialVisibility.RESTRICTED
    contract = documents["ytracker-file:id-1:a1"]
    assert contract.kind is RemoteDocumentKind.FILE
    assert contract.filename == contract.title == "Договор.pdf"
    assert contract.url == "https://tracker.yandex.ru/SUP-1"
    assert contract.locator.startswith(f"https://{TRACKER_HOST}/v3/issues/SUP-1/")
    assert contract.path == "Трекер / Поддержка / SUP-1"
    assert contract.size == len(b"%PDF-1.4 contract")
    assert skips.formats() == {".png": 1}
    assert skips.too_large == {"ytracker-file:id-1:a3"}
    # Очереди — постранично (3 очереди по 2 на страницу).
    pages = [q.get("page") for p, q in server.calls if p == "tracker/v3/queues"]
    assert pages == ["1", "2"]


async def test_issue_search_follows_link_pagination(server: FakeYandex) -> None:
    for number in range(4, 9):
        server.add_issue(f"id-s{number}", f"SUP-{number}", f"Задача {number}")
    documents = await listed(make_adapter(server, tracker_queues="SUP"))
    issues = {k for k in documents if k.startswith("ytracker:")}
    assert issues == {"ytracker:id-1", "ytracker:id-2"} | {
        f"ytracker:id-s{n}" for n in range(4, 9)
    }
    searches = [q for p, q in server.calls if p == "tracker/v3/issues/_search"]
    assert len(searches) == 4
    assert searches[0].get("id") is None and searches[1]["id"] == "id-2"
    assert all(q["expand"] == "attachments" for q in searches)


async def test_queue_filter_skips_listing_queues(server: FakeYandex) -> None:
    documents = await listed(make_adapter(server, tracker_queues="hr, NOPE"))
    assert set(documents) == {"ytracker:id-4"}
    assert not any(p == "tracker/v3/queues" for p, _ in server.calls)


async def test_org_header_for_yandex_360(server: FakeYandex) -> None:
    await listed(make_adapter(server))
    headers = server.tracker_headers[0]
    assert headers["x-org-id"] == ORG_ID
    assert "x-cloud-org-id" not in headers
    assert headers["authorization"] == f"OAuth {ACCESS_TOKEN}"


async def test_cloud_organization_uses_its_header(server: FakeYandex) -> None:
    server.tracker_cloud = True
    adapter = build_adapter(
        {"client_id": CLIENT_ID, "tracker_cloud_org_id": CLOUD_ORG_ID},
        {
            "client_secret": CLIENT_SECRET,
            "access_token": ACCESS_TOKEN,
            "refresh_token": REFRESH_TOKEN,
        },
        server.client(),
        settings(),
    )
    documents = await listed(adapter)
    assert "ytracker:id-1" in documents
    assert server.tracker_headers[0]["x-cloud-org-id"] == CLOUD_ORG_ID
    assert "x-org-id" not in server.tracker_headers[0]


async def test_tracker_without_organization_is_a_config_error(
    server: FakeYandex,
) -> None:
    adapter = build_adapter(
        {"client_id": CLIENT_ID},
        {"client_secret": CLIENT_SECRET, "access_token": ACCESS_TOKEN},
        server.client(),
        settings(),
    )
    with pytest.raises(AdapterConfigError, match="org_id_missing"):
        await listed(adapter)


async def test_new_comment_changes_the_version(server: FakeYandex) -> None:
    before = (await listed(make_adapter(server)))["ytracker:id-2"].version
    server.add_comment("id-2", "Мария Иванова", "Оплачено.")
    after = (await listed(make_adapter(server)))["ytracker:id-2"].version
    assert before != after


async def test_employee_without_tracker_access_gets_nothing(
    server: FakeYandex,
) -> None:
    server.tracker_forbidden_queues.update({"SUP", "HR"})
    assert await listed(make_adapter(server)) == {}


# --- содержимое -------------------------------------------------------------------


async def test_issue_becomes_markdown_with_status_description_and_comments(
    server: FakeYandex,
) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter)

    fetched = await adapter.fetch(documents["ytracker:id-1"], max_bytes=1 << 20)

    assert isinstance(fetched, FetchedMarkdown)
    text = fetched.markdown
    assert text.startswith("# SUP-1: Клиент Альфа: сроки доставки\n")
    assert "Статус: В работе" in text
    assert "Очередь: Поддержка" in text
    assert "Исполнитель: Мария Иванова" in text
    assert "## Описание" in text and "Клиент спрашивает про **сроки**." in text
    assert "{% note" not in text and "Важно." in text
    # Три комментария по два на страницу — все на месте и по порядку.
    assert text.index("Решили: доставка за 3 дня.") < text.index("Закрываю.")
    assert "Клиент согласен." in text
    assert "**Мария Иванова**" in text


async def test_large_issue_is_rejected(server: FakeYandex) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter)
    with pytest.raises(AdapterError, match="document_too_large"):
        await adapter.fetch(documents["ytracker:id-1"], max_bytes=100)


async def test_attachment_is_downloaded_with_the_token(server: FakeYandex) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter)
    fetched = await adapter.fetch(
        documents["ytracker-file:id-1:a1"], max_bytes=MAX_BYTES
    )
    assert fetched == FetchedFile(data=b"%PDF-1.4 contract", filename="Договор.pdf")


async def test_attachment_link_to_another_host_is_refused(server: FakeYandex) -> None:
    adapter = make_adapter(server)
    document = RemoteDocument(
        external_id="ytracker-file:id-1:a1",
        title="x.pdf",
        url="https://tracker.yandex.ru/SUP-1",
        version="1",
        kind=RemoteDocumentKind.FILE,
        module="tracker",
        locator="https://evil.example.com/v3/issues/SUP-1/attachments/a1/x.pdf",
        filename="x.pdf",
    )
    with pytest.raises(AdapterError, match="download_url_foreign"):
        await adapter.fetch(document, max_bytes=MAX_BYTES)


# --- ошибки и лимиты ----------------------------------------------------------------


async def test_expired_token_is_refreshed_once(server: FakeYandex) -> None:
    server.expired.add(ACCESS_TOKEN)
    adapter = make_adapter(server)
    documents = await listed(adapter)
    assert "ytracker:id-1" in documents
    assert adapter.refreshed_credentials is not None


async def test_rate_limit_waits_for_retry_after(
    server: FakeYandex, no_wait: list[float]
) -> None:
    server.tracker_rate_limit_hits = 2
    documents = await listed(make_adapter(server))
    assert "ytracker:id-1" in documents
    assert no_wait == [2.0, 2.0]


async def test_rate_limit_exhausted_is_retryable(server: FakeYandex) -> None:
    server.tracker_rate_limit_hits = 10
    with pytest.raises(AdapterError) as caught:
        await listed(make_adapter(server))
    assert caught.value.code == "rate_limited" and caught.value.retryable
