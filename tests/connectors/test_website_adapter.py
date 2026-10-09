"""Вид website: публичный сайт или справочный центр — карта сайта, обход
ссылок, robots.txt, версии без лишних загрузок, файлы по ссылкам,
вежливость (пауза, Retry-After, свой User-Agent)."""

import gzip
from datetime import UTC, datetime

import pytest

from corp_ed.connectors.base import (
    AdapterConfigError,
    AdapterError,
    FetchedFile,
    FetchedPage,
    RemoteDocument,
)
from corp_ed.connectors.common import counting_skips
from corp_ed.connectors.registry import default_registry
from corp_ed.connectors.website import KIND, SPEC
from corp_ed.connectors.website.adapter import (
    USER_AGENT,
    WebsiteAdapter,
    build_adapter,
)
from corp_ed.core.config import ConnectorSettings
from corp_ed.domain.types import ConnectorMode, MaterialVisibility, RemoteDocumentKind
from tests.connectors.fake_site import BASE, HOST, FakeSite, page

MAX_BYTES = 1024 * 1024
TODAY = datetime(2026, 10, 9, tzinfo=UTC)


class Clock:
    """Часы и сон без настоящего ожидания: сон двигает часы."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_adapter(
    site: FakeSite,
    url: str = f"{BASE}help/",
    *,
    fast: bool = True,
    clock: Clock | None = None,
    **config: str,
) -> WebsiteAdapter:
    clock = clock or Clock()
    return build_adapter(
        {"url": url, **config},
        site.client(),
        max_bytes=MAX_BYTES,
        fast=fast,
        sleep=clock.sleep,
        clock=clock,
        today=lambda: TODAY,
    )


async def listed(
    adapter: WebsiteAdapter, modules: tuple[str, ...] = ("pages", "files")
) -> dict[str, RemoteDocument]:
    return {d.locator: d async for d in adapter.list(list(modules))}


def urlset(*entries: tuple[str, str | None]) -> bytes:
    items = "".join(
        f"<url><loc>{loc}</loc>"
        + (f"<lastmod>{lastmod}</lastmod>" if lastmod else "")
        + "</url>"
        for loc, lastmod in entries
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</urlset>'
    ).encode()


def sitemap_site() -> FakeSite:
    site = FakeSite(
        robots=(
            "User-agent: *\nDisallow: /help/secret\nCrawl-delay: 2\n"
            f"Sitemap: {BASE}sitemap.xml\nSitemap: https://other.ru/sitemap.xml\n"
        )
    )
    site.add(
        "/sitemap.xml",
        (
            '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"<sitemap><loc>{BASE}sitemap-help.xml.gz</loc></sitemap>"
            "</sitemapindex>"
        ).encode(),
        content_type="application/xml",
    )
    site.add(
        "/sitemap-help.xml.gz",
        gzip.compress(
            urlset(
                (f"{BASE}help/", "2026-09-01"),
                (f"{BASE}help/delivery", None),
                (f"http://{HOST}/help/returns", None),
                (f"{BASE}help/secret/plan", "2026-09-01"),
                (f"{BASE}blog/post", "2026-09-01"),
                ("https://other.ru/help/x", "2026-09-01"),
                (f"{BASE}help/offer.pdf", None),
                (f"{BASE}files/other.pdf", None),
                (f"{BASE}help/archive.zip", None),
                (f"{BASE}help/old", None),
            )
        ),
        content_type="application/gzip",
    )
    site.add("/help/", page("Справка", "Всё о сервисе."))
    site.add(
        "/help/delivery",
        page("Доставка", "Доставка — 3 дня."),
        headers={"etag": '"d1"'},
    )
    site.add("/help/returns", page("Возврат", "Возврат — 14 дней."))
    site.add("/help/secret/plan", page("Тайна", "secret"))
    site.add("/blog/post", page("Блог", "Новости."))
    site.add("/files/other.pdf", b"%PDF-1.4 other", content_type="application/pdf")
    site.add(
        "/help/offer.pdf",
        b"%PDF-1.4 offer",
        content_type="application/pdf",
        headers={"last-modified": "Wed, 01 Jul 2026 10:00:00 GMT"},
    )
    site.add("/help/archive.zip", b"PK", content_type="application/zip")
    return site


def crawl_site() -> FakeSite:
    site = FakeSite(robots="User-agent: *\nDisallow: /help/secret\n")
    site.add(
        "/help/",
        page(
            "Справка",
            "Главная справки.",
            links=(
                "/help/a",
                "b?utm_source=mail&x=1",
                "/about",
                "https://other.ru/help/x",
                "/files/price.docx",
                "/img/logo.png",
                "/files/data.zip",
                "/help/secret/1",
                "#top",
                "mailto:help@example.ru",
                "javascript:alert(1)",
                "/help/moved",
                "/help/dup",
            ),
        ),
        headers={"etag": '"root-1"'},
    )
    site.add("/help/a", page("Раздел А", "Текст А.", links=("/help/a/deep",)))
    site.add("/help/a/deep", page("Глубже", "Глубоко.", links=("/help/a/deep/deeper",)))
    site.add("/help/a/deep/deeper", page("Ещё глубже", "Очень глубоко."))
    site.add(
        "/help/b?x=1",
        page(
            "Б",
            "Не индексировать.",
            links=("/help/c",),
            head='<meta name="robots" content="noindex">',
        ),
    )
    site.add(
        "/help/c",
        page(
            "В",
            "Без перехода по ссылкам.",
            links=("/help/d",),
            head='<meta name="kronto-bot" content="nofollow">',
        ),
    )
    site.add("/help/d", page("Г", "Сюда не дойти."))
    site.redirect("/help/moved", "/help/e")
    site.add("/help/e", page("Д", "Переехавшая страница."))
    site.add(
        "/help/dup",
        page("Дубль", "Копия А.", head=f'<link rel="canonical" href="{BASE}help/a">'),
    )
    site.add("/about", page("О нас", "Вне раздела."))
    site.add(
        "/files/price.docx",
        b"PK docx",
        content_type="application/vnd.openxmlformats",
        headers={"etag": '"p1"'},
    )
    site.add("/img/logo.png", b"\x89PNG", content_type="image/png")
    site.add("/files/data.zip", b"PK", content_type="application/zip")
    return site


# --- каталог ----------------------------------------------------------------


def test_spec_is_public_organization_preview() -> None:
    assert SPEC.kind == KIND == "website"
    assert SPEC.mode is ConnectorMode.ORGANIZATION
    assert SPEC.credential_fields == () and SPEC.app_credential_fields == ()
    assert SPEC.preview and SPEC.base
    assert SPEC.url_field == "url"
    assert [m.name for m in SPEC.modules] == ["pages", "files"]
    assert "JavaScript" in SPEC.modules[0].title
    assert [(f.name, f.required) for f in SPEC.config_fields] == [
        ("url", True),
        ("max_pages", False),
        ("max_depth", False),
    ]


@pytest.mark.parametrize(
    ("config", "code"),
    [
        ({"url": BASE}, None),
        ({"url": BASE, "max_pages": "2000", "max_depth": "10"}, None),
        ({"url": BASE, "max_pages": "2001"}, "max_pages_invalid"),
        ({"url": BASE, "max_pages": "0"}, "max_pages_invalid"),
        ({"url": BASE, "max_pages": "много"}, "max_pages_invalid"),
        ({"url": BASE, "max_depth": "11"}, "max_depth_invalid"),
        ({"url": f"{BASE}help/?lang=ru"}, "url_query_not_supported"),
    ],
)
def test_config_check(config: dict[str, str], code: str | None) -> None:
    assert SPEC.config_check is not None
    assert SPEC.config_check(config) == code


def test_kind_is_hidden_until_checked_live() -> None:
    assert "website" not in [
        s.kind for s in default_registry(ConnectorSettings()).kinds()
    ]
    enabled = default_registry(ConnectorSettings(preview_kinds="website"))
    assert enabled.offered("website")
    adapter = enabled.build("website", {"url": BASE}, {}, FakeSite().client())
    assert isinstance(adapter, WebsiteAdapter)


# --- карта сайта ---------------------------------------------------------------


async def test_sitemap_lists_scope_with_versions_and_public_visibility() -> None:
    site = sitemap_site()
    with counting_skips() as skips:
        documents = await listed(make_adapter(site))

    assert set(documents) == {
        f"{BASE}help/",
        f"{BASE}help/delivery",
        f"{BASE}help/returns",
        f"{BASE}help/offer.pdf",
    }
    root = documents[f"{BASE}help/"]
    assert root.external_id == f"web:{BASE}help/"
    assert root.kind is RemoteDocumentKind.PAGE and root.module == "pages"
    assert root.version == "lastmod:2026-09-01"
    assert root.url == f"{BASE}help/"
    assert root.path == f"{HOST}/help/"
    assert documents[f"{BASE}help/delivery"].version == 'etag:"d1"'
    # Ни ETag, ни Last-Modified: страница скачана при обходе, версия — хеш.
    returns = documents[f"{BASE}help/returns"]
    assert returns.version.startswith("sha:")
    assert returns.title == "Возврат"
    offer = documents[f"{BASE}help/offer.pdf"]
    assert offer.kind is RemoteDocumentKind.FILE and offer.module == "files"
    assert offer.filename == "offer.pdf" and offer.title == "offer.pdf"
    assert offer.version == "lm:Wed, 01 Jul 2026 10:00:00 GMT"
    assert offer.size == len(b"%PDF-1.4 offer")
    for document in documents.values():
        # Публичный сайт: прав в источнике нет — видит вся компания.
        assert document.visibility is MaterialVisibility.TENANT
        assert document.allowed_emails == frozenset()
    assert skips.formats() == {".zip": 1}
    # Файл карты вне раздела — не документ раздела.
    assert "/files/other.pdf" not in site.calls()
    # Страница с lastmod не запрашивалась; запрещённая robots — тоже.
    assert "/help/" not in site.calls()
    assert not any("secret" in path for path in site.calls())
    assert site.agents == {USER_AGENT}
    assert "kronto-bot" in USER_AGENT and "krontoai.ru" in USER_AGENT


async def test_sitemap_page_fetch_returns_html_and_title() -> None:
    site = sitemap_site()
    adapter = make_adapter(site)
    documents = await listed(adapter)
    root = documents[f"{BASE}help/"]
    # Заголовок страницы с lastmod ещё неизвестен — из адреса.
    assert root.title == "help"

    fetched = await adapter.fetch(root, max_bytes=MAX_BYTES)

    assert isinstance(fetched, FetchedPage)
    assert "Всё о сервисе." in fetched.html
    assert fetched.title == "Справка"


async def test_page_downloaded_while_listing_is_not_downloaded_again() -> None:
    site = sitemap_site()
    adapter = make_adapter(site)
    documents = await listed(adapter)
    before = site.calls("GET").count("/help/returns")

    fetched = await adapter.fetch(documents[f"{BASE}help/returns"], max_bytes=MAX_BYTES)

    assert isinstance(fetched, FetchedPage) and "14 дней" in fetched.html
    assert site.calls("GET").count("/help/returns") == before == 1


async def test_file_fetch_downloads_bytes() -> None:
    site = sitemap_site()
    adapter = make_adapter(site)
    documents = await listed(adapter)
    fetched = await adapter.fetch(
        documents[f"{BASE}help/offer.pdf"], max_bytes=MAX_BYTES
    )
    assert fetched == FetchedFile(data=b"%PDF-1.4 offer", filename="offer.pdf")


async def test_file_served_as_html_is_not_a_file() -> None:
    site = sitemap_site()
    adapter = make_adapter(site)
    documents = await listed(adapter)
    site.add("/help/offer.pdf", b"<html>login</html>")
    with pytest.raises(AdapterError, match="download_failed"):
        await adapter.fetch(documents[f"{BASE}help/offer.pdf"], max_bytes=MAX_BYTES)


async def test_vanished_page_fetch_is_not_found() -> None:
    site = sitemap_site()
    adapter = make_adapter(site)
    documents = await listed(adapter)
    del site.resources["/help/"]
    with pytest.raises(AdapterError, match="not_found"):
        await adapter.fetch(documents[f"{BASE}help/"], max_bytes=MAX_BYTES)


async def test_head_not_supported_falls_back() -> None:
    site = sitemap_site()
    site.head_supported = False
    documents = await listed(make_adapter(site))
    assert documents[f"{BASE}help/delivery"].version == 'etag:"d1"'
    assert documents[f"{BASE}help/offer.pdf"].version == "day:2026-10-09"


async def test_only_selected_modules() -> None:
    site = sitemap_site()
    pages = await listed(make_adapter(site), ("pages",))
    assert all(d.kind is RemoteDocumentKind.PAGE for d in pages.values())
    files = await listed(make_adapter(sitemap_site()), ("files",))
    assert set(files) == {f"{BASE}help/offer.pdf"}


async def test_page_cap_counts_addresses() -> None:
    documents = await listed(make_adapter(sitemap_site(), max_pages="2"))
    assert len(documents) == 2


async def test_sitemap_without_scope_entries_falls_back_to_crawl() -> None:
    site = crawl_site()
    site.robots = f"User-agent: *\nSitemap: {BASE}sitemap.xml\n"
    site.add("/sitemap.xml", urlset((f"{BASE}blog/1", None)))
    documents = await listed(make_adapter(site))
    assert f"{BASE}help/a" in documents


async def test_broken_sitemap_falls_back_to_crawl() -> None:
    site = crawl_site()
    site.add("/sitemap.xml", b"<html>not a sitemap</html>")
    documents = await listed(make_adapter(site))
    assert f"{BASE}help/a" in documents


# --- обход ссылок ----------------------------------------------------------------


async def test_crawl_follows_links_in_scope() -> None:
    site = crawl_site()
    with counting_skips() as skips:
        documents = await listed(make_adapter(site, max_depth="2"))

    assert set(documents) == {
        f"{BASE}help/",
        f"{BASE}help/a",
        f"{BASE}help/a/deep",
        f"{BASE}help/c",
        f"{BASE}help/e",
        f"{BASE}files/price.docx",
    }
    root = documents[f"{BASE}help/"]
    assert root.title == "Справка"
    assert root.version == 'etag:"root-1"'
    assert documents[f"{BASE}help/a"].version.startswith("sha:")
    price = documents[f"{BASE}files/price.docx"]
    assert price.kind is RemoteDocumentKind.FILE and price.version == 'etag:"p1"'
    # Картинки — не документы; архив — неподдерживаемый формат.
    assert skips.formats() == {".zip": 1}
    calls = site.calls()
    # Глубже max_depth, по nofollow, вне раздела и запрещённое — не запрашивались.
    for path in ("/help/a/deep/deeper", "/help/d", "/about", "/help/secret/1"):
        assert path not in calls
    assert "/img/logo.png" not in calls
    # Метки рассылок отрезаны: страница запрошена без них.
    assert "/help/b?x=1" in calls
    assert not any("utm_" in path for path in calls)


async def test_crawl_pages_are_served_from_cache() -> None:
    site = crawl_site()
    adapter = make_adapter(site)
    documents = await listed(adapter)
    before = len(site.calls("GET"))
    fetched = await adapter.fetch(documents[f"{BASE}help/e"], max_bytes=MAX_BYTES)
    assert isinstance(fetched, FetchedPage) and fetched.title == "Д"
    assert len(site.calls("GET")) == before


async def test_crawl_page_cap() -> None:
    site = crawl_site()
    documents = await listed(make_adapter(site, max_pages="3"), ("pages",))
    assert len(documents) <= 3
    assert len([p for p in site.calls("GET") if p.startswith("/help")]) <= 3


async def test_x_robots_tag_noindex_header() -> None:
    site = crawl_site()
    site.resources["/help/a"].headers["x-robots-tag"] = "noindex"
    documents = await listed(make_adapter(site))
    assert f"{BASE}help/a" not in documents
    # nofollow не указан: по ссылкам страницы обход идёт.
    assert f"{BASE}help/a/deep" in documents


async def test_server_error_page_is_skipped() -> None:
    site = crawl_site()
    site.resources["/help/a"].status = 500
    documents = await listed(make_adapter(site))
    assert f"{BASE}help/a" not in documents
    assert f"{BASE}help/e" in documents


# --- robots.txt и проверка --------------------------------------------------------


async def test_missing_robots_allows_everything() -> None:
    site = crawl_site()
    site.robots = None
    documents = await listed(make_adapter(site))
    assert f"{BASE}help/secret/1" not in documents  # ссылка есть, страницы нет
    assert "/help/secret/1" in site.calls()


async def test_robots_server_error_stops_the_run_retryably() -> None:
    site = crawl_site()
    site.robots = 503
    with pytest.raises(AdapterError) as caught:
        await listed(make_adapter(site))
    assert caught.value.code == "robots_unavailable" and caught.value.retryable


async def test_check_reports_robots_disallow() -> None:
    site = crawl_site()
    site.robots = "User-agent: kronto-bot\nDisallow: /\n"
    with pytest.raises(AdapterConfigError, match="robots_disallowed"):
        await make_adapter(site).check()


async def test_check_follows_redirect_into_scope() -> None:
    site = crawl_site()
    site.redirect("/help", "/help/")
    await make_adapter(site, f"{BASE}help").check()


async def test_check_detects_javascript_only_site() -> None:
    site = FakeSite()
    site.add(
        "/",
        b"<html><head><script src='/app.js'></script></head>"
        b"<body><div id='root'></div></body></html>",
    )
    with pytest.raises(AdapterError, match="site_needs_javascript"):
        await make_adapter(site, BASE).check()


async def test_check_reports_missing_start_page() -> None:
    with pytest.raises(AdapterError, match="not_found"):
        await make_adapter(FakeSite(), f"{BASE}nope/").check()


# --- вежливость ---------------------------------------------------------------------


async def test_pause_between_requests_honours_crawl_delay() -> None:
    site = sitemap_site()
    clock = Clock()
    await listed(make_adapter(site, fast=False, clock=clock))
    requests = len(site.log)
    # Crawl-delay: 2 — между запросами не меньше двух секунд.
    assert len(clock.sleeps) >= requests - 2
    assert all(pause <= 2.0 for pause in clock.sleeps)
    assert sum(clock.sleeps) >= 2.0 * (requests - 2)


async def test_default_pause_without_crawl_delay() -> None:
    site = crawl_site()
    clock = Clock()
    await listed(make_adapter(site, fast=False, clock=clock), ("pages",))
    assert clock.sleeps and max(clock.sleeps) == pytest.approx(1.0)


async def test_huge_crawl_delay_is_capped() -> None:
    site = crawl_site()
    site.robots = "User-agent: *\nCrawl-delay: 3600\n"
    clock = Clock()
    await listed(make_adapter(site, fast=False, clock=clock), ("pages",))
    assert max(clock.sleeps) == pytest.approx(10.0)


async def test_rate_limit_waits_for_retry_after() -> None:
    site = crawl_site()
    site.rate_limit["/help/a"] = 2
    clock = Clock()
    documents = await listed(make_adapter(site, clock=clock))
    assert f"{BASE}help/a" in documents
    assert clock.sleeps.count(3.0) == 2


async def test_rate_limit_exhausted_is_retryable() -> None:
    site = crawl_site()
    site.rate_limit["/help/a"] = 10
    with pytest.raises(AdapterError) as caught:
        await listed(make_adapter(site))
    assert caught.value.code == "rate_limited" and caught.value.retryable


async def test_foreign_locator_is_never_requested() -> None:
    site = crawl_site()
    adapter = make_adapter(site)
    foreign = RemoteDocument(
        external_id="web:https://evil.ru/x.pdf",
        title="x.pdf",
        url="https://evil.ru/x.pdf",
        version="1",
        kind=RemoteDocumentKind.FILE,
        module="files",
        locator="https://evil.ru/x.pdf",
        filename="x.pdf",
    )
    with pytest.raises(AdapterError, match="url_foreign"):
        await adapter.fetch(foreign, max_bytes=MAX_BYTES)
    assert site.log == []
