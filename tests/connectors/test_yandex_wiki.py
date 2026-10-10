"""Модуль Вики вида yandex360: обход разделов от имени сотрудника,
Markdown страниц, ошибки API Вики."""

import pytest
from cryptography.fernet import Fernet

from corp_ed.connectors.base import (
    AdapterAuthError,
    AdapterConfigError,
    AdapterError,
    FetchedMarkdown,
    RemoteDocument,
)
from corp_ed.connectors.yandex.adapter import YandexAdapter, build_adapter
from corp_ed.connectors.yandex.wiki import normalize_wiki_markup, parse_roots
from corp_ed.core import outbound
from corp_ed.core.config import ConnectorSettings
from corp_ed.domain.types import RemoteDocumentKind
from tests.connectors.fake_yandex import (
    ACCESS_TOKEN,
    CLIENT_ID,
    CLIENT_SECRET,
    DISK_API,
    OAUTH_SERVER,
    ORG_ID,
    REFRESH_TOKEN,
    WIKI_API,
    FakeYandex,
    sample_yandex,
)

KEY = Fernet.generate_key().decode()
MAX_BYTES = 1024 * 1024


def make_adapter(server: FakeYandex, **config: str) -> YandexAdapter:
    settings = ConnectorSettings(
        secrets_keys=KEY,
        yandex_oauth_server=OAUTH_SERVER,
        yandex_disk_api=DISK_API,
        yandex_wiki_api=WIKI_API,
        max_document_bytes=MAX_BYTES,
        max_large_document_bytes=MAX_BYTES,
    )  # type: ignore[arg-type]
    return build_adapter(
        {"client_id": CLIENT_ID, "org_id": ORG_ID, **config},
        {
            "client_secret": CLIENT_SECRET,
            "access_token": ACCESS_TOKEN,
            "refresh_token": REFRESH_TOKEN,
            "expires_at": "0",
        },
        server.client(),
        settings,
    )


async def listed(adapter: YandexAdapter) -> dict[str, RemoteDocument]:
    return {d.external_id: d async for d in adapter.list(["wiki"])}


@pytest.fixture
def server() -> FakeYandex:
    return sample_yandex()


async def test_default_root_walks_visible_published_text_pages(
    server: FakeYandex,
) -> None:
    documents = await listed(make_adapter(server))
    # Черновик, динамическая таблица, редирект и скрытая от сотрудника
    # страница не попадают; раздел sales — не под главной.
    assert set(documents) == {"ywiki:1", "ywiki:2"}
    hr = documents["ywiki:2"]
    assert hr.kind is RemoteDocumentKind.PAGE
    assert hr.module == "wiki"
    assert hr.title == "Кадры"
    assert hr.url == "https://wiki.yandex.ru/homepage/hr/"
    assert hr.version == "2026-06-01T10:00:00+03:00"
    assert hr.path == "Главная / Кадры"
    assert hr.locator == "2"
    assert hr.modified_at is not None


async def test_every_wiki_request_carries_the_organization(server: FakeYandex) -> None:
    await listed(make_adapter(server))
    assert server.wiki_headers
    for headers in server.wiki_headers:
        assert headers["x-org-id"] == ORG_ID
        assert headers["authorization"] == f"OAuth {ACCESS_TOKEN}"


async def test_configured_roots_are_walked_once_each(server: FakeYandex) -> None:
    adapter = make_adapter(
        server,
        wiki_roots="https://wiki.yandex.ru/sales/, homepage/hr, homepage, нет-такого",
    )
    documents = await listed(adapter)
    assert set(documents) == {"ywiki:7", "ywiki:2", "ywiki:1"}
    # Страница, попавшая в два раздела, читается один раз.
    details = [p for p, _ in server.calls if p == "wiki/v1/pages/2"]
    assert details == ["wiki/v1/pages/2"]


async def test_descendants_are_paged_by_cursor(server: FakeYandex) -> None:
    for index in range(10, 260):
        server.add_wiki_page(index, f"homepage/p{index}", f"Страница {index}", "текст")
    documents = await listed(make_adapter(server))
    assert len(documents) == 252
    cursors = [
        q.get("cursor") for p, q in server.calls if p == "wiki/v1/pages/descendants"
    ]
    assert cursors == [None, "100", "200"]


async def test_fetch_returns_normalized_markdown(server: FakeYandex) -> None:
    adapter = make_adapter(server, wiki_roots="sales, homepage/hr")
    documents = await listed(adapter)
    hr = await adapter.fetch(documents["ywiki:2"], max_bytes=MAX_BYTES)
    assert isinstance(hr, FetchedMarkdown)
    assert "| Документ | Срок |" in hr.markdown
    assert "| Отпуск | 14 дней |" in hr.markdown
    assert "#|" not in hr.markdown
    sales = await adapter.fetch(documents["ywiki:7"], max_bytes=MAX_BYTES)
    assert isinstance(sales, FetchedMarkdown)
    assert "Скидка — 5 %." in sales.markdown
    assert "Прайс.pdf" in sales.markdown
    assert "{%" not in sales.markdown and "{{" not in sales.markdown


async def test_page_id_is_one_path_segment(server: FakeYandex) -> None:
    """locator с /, ? и # не уводит запрос на другую страницу Вики."""
    adapter = make_adapter(server)
    for locator in ("7/../1", "1?fields=content#x", "../pages/1", ".."):
        document = RemoteDocument(
            external_id=f"ywiki:{locator}",
            title="",
            url="",
            version="",
            kind=RemoteDocumentKind.PAGE,
            module="wiki",
            locator=locator,
        )
        with pytest.raises(AdapterError, match="not_found"):
            await adapter.fetch(document, max_bytes=MAX_BYTES)


async def test_fetch_refuses_oversized_pages(server: FakeYandex) -> None:
    adapter = make_adapter(server)
    documents = await listed(adapter)
    with pytest.raises(AdapterError, match="document_too_large"):
        await adapter.fetch(documents["ywiki:1"], max_bytes=5)


async def test_oversized_api_response_is_an_adapter_error(
    server: FakeYandex, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ответ Вики больше потолка — ошибка источника, а не сбой воркера."""
    monkeypatch.setattr(outbound, "MAX_RESPONSE_BYTES", 16)
    with pytest.raises(AdapterError) as excinfo:
        await listed(make_adapter(server))
    assert excinfo.value.code == "response_too_large"
    assert not excinfo.value.retryable


async def test_unknown_error_code_from_the_wiki_is_scrubbed(
    server: FakeYandex,
) -> None:
    """Код ошибки из ответа Вики уходит в API, аудит и интерфейс —
    только латиница, цифры и _, не длиннее 64."""
    server.api_error = (400, {"error_code": "Odd Error\n<b>", "debug_message": "…"})
    with pytest.raises(AdapterError) as excinfo:
        await listed(make_adapter(server))
    assert excinfo.value.code == "wiki_odd_error__b_"


async def test_employee_who_never_opened_the_wiki_gets_a_grant_error(
    server: FakeYandex,
) -> None:
    """403 FORCED_SYNC_REQUIRED: сотруднику открыть Вики в браузере —
    это его грант, а не приложение."""
    server.wiki_needs_sync = True
    with pytest.raises(AdapterAuthError, match="wiki_login_required"):
        await listed(make_adapter(server))


async def test_wiki_needs_the_organization_id(server: FakeYandex) -> None:
    with pytest.raises(AdapterConfigError, match="org_id_missing"):
        await listed(make_adapter(server, org_id=""))


async def test_expired_token_is_refreshed_for_the_wiki_too(server: FakeYandex) -> None:
    server.expired.add(ACCESS_TOKEN)
    adapter = make_adapter(server)
    documents = await listed(adapter)
    assert "ywiki:2" in documents
    assert adapter.refreshed_credentials is not None


def test_parse_roots() -> None:
    assert parse_roots("") == ()
    assert parse_roots(" homepage/hr/ ,https://wiki.yandex.ru/sales/\nhomepage/hr") == (
        "homepage/hr",
        "sales",
    )


def test_normalize_wiki_markup() -> None:
    text = (
        "# Заголовок\n\n"
        '{% cut "Подробнее" %}\n\nВнутри.\n\n{% endcut %}\n\n\n\n'
        "#|\n|| a | b ||\n|| 1\nпродолжение | 2 ||\n|#\n"
        '{{grid page="x"}}\n'
    )
    result = normalize_wiki_markup(text)
    assert result.startswith("# Заголовок\n")
    assert "Внутри." in result
    assert "{%" not in result and "{{" not in result
    assert "| a | b |\n| --- | --- |\n| 1 продолжение | 2 |" in result
    assert "\n\n\n" not in result
