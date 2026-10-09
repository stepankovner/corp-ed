"""Спецификация вида website и адаптер: публичный сайт или справочный центр.

Режим organization без учётных данных: админ указывает адрес сайта или
раздела (путь адреса — префикс: https://company.ru/help/ → обходятся
только страницы под /help/). Ключей нет — подключение готово сразу.

Права. Содержимое публичное, у сайта нет списка тех, кто его видит, —
документы видит вся компания (MaterialVisibility.TENANT). Подключать так
можно только то, что открыто без входа: закрытые разделы (401/403) не
читаются, а cookie и логины вид не принимает.

Обход:
1. robots.txt (RFC 9309, robots.py): правила группы kronto-bot или *,
   Crawl-delay — пауза между запросами; 4xx — можно всё; 5xx и сбой —
   запуск прерывается (RFC: недоступный robots.txt — «нельзя ничего»);
2. карты сайта: из строк Sitemap в robots.txt, иначе /sitemap.xml;
   индексы карт, gzip (sitemap.py). В обход идут адреса того же хоста под
   префиксом, разрешённые robots.txt, не больше max_pages;
3. карты нет или в ней нет адресов раздела — обход ссылок от начального
   адреса вширь: тот же хост и префикс, глубина max_depth, не больше
   max_pages запросов. Ссылки на файлы поддерживаемых форматов на том же
   хосте (pdf, docx, …) — документы модуля files, даже вне префикса;
   прочие форматы (zip, odt) считаются пропущенными. Метки рассылок
   (utm_*, yclid, …) отрезаются. Учитываются meta robots и X-Robots-Tag
   (noindex — не документ, nofollow — не идти по ссылкам), rel=nofollow
   у ссылки и rel=canonical (дубль — не документ). JavaScript не
   выполняется: сайт-приложение, у которого текст строит скрипт, не
   читается (check сообщает site_needs_javascript).

Инкрементальность. Адаптер собирается заново на каждый запуск и прошлых
версий не помнит, поэтому условный GET (If-None-Match) ему не с чем
делать; его роль играет version, которую ядро сравнивает с прошлой:
- lastmod из карты — версия без единого запроса при обходе;
- нет lastmod — HEAD и версия из ETag или Last-Modified: тело не
  качается, пока сайт не сообщит об изменении;
- нет и их (или HEAD не поддержан) — страница скачивается при обходе и
  версия — хеш тела; fetch сразу после обхода берёт её из кеша, а не
  качает второй раз. Файл без валидаторов — версия по дню: перекачается
  не чаще раза в сутки (ядро не переиндексирует неизменённый файл).
Пропавшие из карты или из обхода страницы ядро удаляет, если обход дошёл
до конца. Обход должен укладываться в CONNECTOR_MAX_RUN_MINUTES (20):
при паузе 1 с это около 1 000 запросов — поэтому по умолчанию 500
адресов, потолок 2 000 (для большого сайта нужна карта с lastmod).
"""

import asyncio
import hashlib
import re
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import httpx
import structlog
from bs4 import BeautifulSoup, Tag
from bs4.dammit import UnicodeDammit

from corp_ed.connectors.base import (
    AdapterConfigError,
    AdapterError,
    AdapterOptions,
    FetchedContent,
    FetchedFile,
    FetchedPage,
    RemoteDocument,
)
from corp_ed.connectors.common import note_too_large, note_unsupported, to_int
from corp_ed.connectors.html import html_to_markdown
from corp_ed.connectors.registry import AdapterRegistry, FieldSpec, KindSpec, ModuleSpec
from corp_ed.connectors.website.client import (
    MIN_INTERVAL,
    ROBOTS_AGENT,
    USER_AGENT,
    SiteClient,
    Sleep,
)
from corp_ed.connectors.website.robots import (
    ALLOW_ALL,
    MAX_ROBOTS_BYTES,
    RobotsRules,
    parse_robots,
)
from corp_ed.connectors.website.sitemap import (
    MAX_SITEMAP_BYTES,
    Sitemap,
    SitemapError,
    parse_sitemap,
    unpack,
)
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.outbound import MAX_URL_LENGTH, OutboundClient, OutboundTooLargeError
from corp_ed.domain.types import ConnectorMode, MaterialVisibility, RemoteDocumentKind
from corp_ed.ingest.extract import ExtractionError, supported_extensions

__all__ = ["KIND", "SPEC", "USER_AGENT", "WebsiteAdapter", "build_adapter", "register"]

logger = structlog.get_logger()

KIND = "website"
MODULE_PAGES = "pages"
MODULE_FILES = "files"
PREFIX = "web:"
DEFAULT_MAX_PAGES = 500
MAX_PAGES = 2000
DEFAULT_MAX_DEPTH = 5
MAX_DEPTH = 10
PAGE_MAX_BYTES = 5 * 1024 * 1024
"""Страница справки — десятки килобайт; больше 5 МиБ — не текст для ответов."""
ROBOTS_FETCH_BYTES = 2 * 1024 * 1024
MAX_SITEMAPS = 50
MAX_SITEMAP_DEPTH = 3
MAX_START_REDIRECTS = 3
CACHE_SIZE = 8
_EXTERNAL_ID_LIMIT = 500
# Метод list адаптера заслоняет встроенный list в теле класса.
_Entries = list[tuple[str, str | None]]

# Адреса, которые не страницы и не документы: оформление и медиа.
_ASSET_EXTENSIONS = frozenset(
    {
        ".css", ".js", ".mjs", ".map", ".json", ".xml", ".rss", ".atom",
        ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".avif", ".ico",
        ".bmp", ".tif", ".tiff", ".woff", ".woff2", ".ttf", ".otf", ".eot",
        ".mp3", ".mp4", ".m4a", ".ogg", ".wav", ".webm", ".avi", ".mov",
        ".mkv", ".flv", ".wmv",
    }
)  # fmt: skip
# Документы и архивы форматов, которых ассистент не читает: админ видит
# их в «пропущено», а не гадает, почему прайс в ODS не попал в ответы.
_UNSUPPORTED_EXTENSIONS = frozenset(
    {
        ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz",
        ".odt", ".ods", ".odp", ".rtf", ".csv", ".xls", ".ppt", ".pps",
        ".ppsx", ".epub", ".djvu", ".djv", ".fb2", ".exe", ".msi", ".dmg",
        ".apk", ".iso", ".dwg", ".xps", ".pages", ".numbers", ".key",
    }
)  # fmt: skip
_TRACKING_PARAMS = frozenset({"yclid", "gclid", "fbclid", "ysclid", "_openstat"})
_SAFE_PATH = "!$&'()*+,;=:@/%~-._"
_SAFE_QUERY = _SAFE_PATH + "?"
_SPACES = re.compile(r"\s+")


def _bounded_int(value: str | None, ceiling: int) -> int | None:
    """Число из поля формы в пределах 1..ceiling; не число — None."""
    text = (value or "").strip()
    if not text.isdigit():
        return None
    number = int(text)
    return number if 1 <= number <= ceiling else None


def _check_config(config: Mapping[str, str]) -> str | None:
    try:
        parts = urlsplit(config.get("url", ""))
    except ValueError:
        return "url_invalid"
    if parts.query:
        # Раздел задаётся путём; адрес с параметрами — не раздел, а одна
        # выдача поиска или фильтра.
        return "url_query_not_supported"
    if (
        config.get("max_pages", "").strip()
        and _bounded_int(config["max_pages"], MAX_PAGES) is None
    ):
        return "max_pages_invalid"
    if (
        config.get("max_depth", "").strip()
        and _bounded_int(config["max_depth"], MAX_DEPTH) is None
    ):
        return "max_depth_invalid"
    return None


SPEC = KindSpec(
    kind=KIND,
    title="Публичный сайт или справочный центр",
    mode=ConnectorMode.ORGANIZATION,
    modules=(
        ModuleSpec(
            MODULE_PAGES,
            "Страницы сайта (HTML; сайты, где текст строит JavaScript, не читаются)",
        ),
        ModuleSpec(
            MODULE_FILES,
            "Файлы по ссылкам со страниц (pdf, docx, doc, xlsx, pptx, txt, md)",
        ),
    ),
    config_fields=(
        FieldSpec(
            "url",
            "Адрес сайта или раздела (https://company.ru/help/): обходятся "
            "страницы под этим адресом, открытые без входа",
        ),
        FieldSpec(
            "max_pages",
            f"Сколько адресов обходить, до {MAX_PAGES} (пусто — {DEFAULT_MAX_PAGES})",
            required=False,
        ),
        FieldSpec(
            "max_depth",
            f"Глубина перехода по ссылкам, если у сайта нет sitemap.xml, до "
            f"{MAX_DEPTH} (пусто — {DEFAULT_MAX_DEPTH})",
            required=False,
        ),
    ),
    credential_fields=(),
    url_field="url",
    config_check=_check_config,
    extra={"user_agent": USER_AGENT, "robots_agent": ROBOTS_AGENT},
    preview=True,
)


# --- адреса ----------------------------------------------------------------------


@dataclass(frozen=True)
class Scope:
    """Сайт и раздел подключения: что считать «своим» адресом."""

    netloc: str
    prefix: str
    start: str

    @classmethod
    def from_url(cls, url: str) -> "Scope":
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
        if not host:
            raise AdapterConfigError("url_invalid")
        port = parts.port
        netloc = host if port in (None, 443) else f"{host}:{port}"
        path = quote(parts.path or "/", safe=_SAFE_PATH)
        prefix = path if path.endswith("/") else path + "/"
        return cls(netloc, prefix, urlunsplit(("https", netloc, path, "", "")))

    @property
    def origin(self) -> str:
        return f"https://{self.netloc}"

    def normalize(self, href: str, base: str) -> str | None:
        """Ссылка → абсолютный https-адрес этого сайта без фрагмента и меток
        рассылок; ссылка на другой сайт, mailto:, javascript: — None.

        http того же хоста поднимается до https: карты сайтов нередко
        перечисляют http-адреса сайта, который давно на https, а сами мы
        ходим только по https.
        """
        try:
            parts = urlsplit(urljoin(base, href.strip()))
            port = parts.port
        except ValueError:
            return None
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https") or parts.username or parts.password:
            return None
        host = (parts.hostname or "").lower()
        default = 443 if scheme == "https" else 80
        netloc = host if port in (None, default) else f"{host}:{port}"
        if netloc != self.netloc:
            return None
        path = quote(parts.path or "/", safe=_SAFE_PATH)
        query = quote(_strip_tracking(parts.query), safe=_SAFE_QUERY)
        url = urlunsplit(("https", netloc, path, query, ""))
        return url if len(url) <= MAX_URL_LENGTH else None

    def in_prefix(self, url: str) -> bool:
        path = urlsplit(url).path
        return path.startswith(self.prefix) or path == self.prefix.rstrip("/")

    def owns(self, url: str) -> bool:
        parts = urlsplit(url)
        return parts.scheme == "https" and parts.netloc.lower() == self.netloc


def _strip_tracking(query: str) -> str:
    kept = [
        pair
        for pair in query.split("&")
        if pair
        and not (name := pair.split("=", 1)[0].lower()).startswith("utm_")
        and name not in _TRACKING_PARAMS
    ]
    return "&".join(kept)


def _request_path(url: str) -> str:
    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def _extension(url: str) -> str:
    path = unquote(urlsplit(url).path)
    return "" if path.endswith("/") else PurePosixPath(path).suffix.lower()


def _kind(url: str) -> str:
    """file — документ поддерживаемого формата; unsupported — документ,
    которого ассистент не читает; asset — оформление и медиа; page — всё
    прочее (что это на самом деле, скажет Content-Type ответа)."""
    ext = _extension(url)
    if ext in supported_extensions():
        return "file"
    if ext in _UNSUPPORTED_EXTENSIONS:
        return "unsupported"
    if ext in _ASSET_EXTENSIONS:
        return "asset"
    return "page"


def _filename(url: str) -> str:
    return unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]) or "file"


def _title_from_url(url: str) -> str:
    parts = urlsplit(url)
    segment = unquote(parts.path.rstrip("/").rsplit("/", 1)[-1])
    stem = PurePosixPath(segment).stem if _extension(url) else segment
    title = _SPACES.sub(" ", re.sub(r"[-_]+", " ", stem)).strip()
    return title or parts.netloc


def _external_id(url: str) -> str:
    if len(url) <= _EXTERNAL_ID_LIMIT:
        return f"{PREFIX}{url}"
    return f"{PREFIX}#{hashlib.sha256(url.encode()).hexdigest()}"


def _short(version: str) -> str:
    """Версия в колонку на 128 символов: длинный ETag — хешем."""
    if len(version) <= 120:
        return version
    return "h:" + hashlib.sha256(version.encode()).hexdigest()[:40]


def _validator(headers: httpx.Headers) -> str | None:
    etag = headers.get("etag", "").strip()
    if etag:
        return _short(f"etag:{etag}")
    modified = headers.get("last-modified", "").strip()
    if modified:
        return _short(f"lm:{modified}")
    return None


def _is_html(response: httpx.Response) -> bool:
    content_type = str(response.headers.get("content-type", "")).lower()
    if content_type:
        return content_type.startswith(("text/html", "application/xhtml+xml"))
    return b"<html" in response.content[:2048].lower()


def _decode(response: httpx.Response) -> str:
    declared = response.charset_encoding
    dammit = UnicodeDammit(
        response.content, [declared] if declared else [], is_html=True
    )
    return dammit.unicode_markup or response.content.decode("utf-8", "replace")


def _robots_header(headers: httpx.Headers) -> set[str]:
    """X-Robots-Tag: директивы для всех и для kronto-bot; чужие — мимо."""
    directives: set[str] = set()
    for value in headers.get_list("x-robots-tag"):
        text = value.strip().lower()
        agent, sep, rest = text.partition(":")
        if (
            sep
            and " " not in agent.strip()
            and agent.strip() not in ("unavailable_after",)
        ):
            if agent.strip() != ROBOTS_AGENT:
                continue
            text = rest
        directives.update(part.strip() for part in text.split(","))
    return directives


# --- страница ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Page:
    url: str
    html: str
    title: str
    links: tuple[str, ...]
    canonical: str | None
    noindex: bool
    nofollow: bool
    version: str


def _parse_page(url: str, response: httpx.Response) -> _Page:
    html = _decode(response)
    soup = BeautifulSoup(html, "html.parser")
    base = url
    base_tag = soup.find("base", href=True)
    if isinstance(base_tag, Tag):
        base = urljoin(url, str(base_tag["href"]))
    directives = _robots_header(response.headers)
    for meta in soup.find_all("meta"):
        if not isinstance(meta, Tag):
            continue
        name = str(meta.get("name") or "").strip().lower()
        if name in ("robots", ROBOTS_AGENT):
            content = str(meta.get("content") or "").lower()
            directives.update(part.strip() for part in content.split(","))
    title = ""
    if soup.title is not None and soup.title.string:
        title = str(soup.title.string)
    else:
        heading = soup.find("h1")
        if heading is not None:
            title = heading.get_text(" ")
    links: list[str] = []
    for anchor in soup.find_all(["a", "area"], href=True):
        if not isinstance(anchor, Tag):
            continue
        rel = anchor.get_attribute_list("rel")
        if "nofollow" in [str(item).lower() for item in rel if item]:
            continue
        links.append(urljoin(base, str(anchor["href"])))
    canonical: str | None = None
    for link in soup.find_all("link", href=True):
        if isinstance(link, Tag) and "canonical" in [
            str(item).lower() for item in link.get_attribute_list("rel") if item
        ]:
            canonical = urljoin(base, str(link["href"]))
            break
    version = _validator(response.headers) or (
        "sha:" + hashlib.sha256(response.content).hexdigest()[:40]
    )
    return _Page(
        url=url,
        html=html,
        title=_SPACES.sub(" ", title).strip()[:200],
        links=tuple(links),
        canonical=canonical,
        noindex=bool(directives & {"noindex", "none"}),
        nofollow=bool(directives & {"nofollow", "none"}),
        version=version,
    )


# --- адаптер -----------------------------------------------------------------------


class WebsiteAdapter:
    def __init__(
        self,
        client: SiteClient,
        scope: Scope,
        *,
        max_pages: int,
        max_depth: int,
        max_bytes: int,
        today: Callable[[], datetime],
    ) -> None:
        self._client = client
        self._scope = scope
        self._max_pages = max_pages
        self._max_depth = max_depth
        self._max_bytes = max_bytes
        self._today = today
        self._robots: RobotsRules | None = None
        self._cache: OrderedDict[str, _Page] = OrderedDict()

    async def check(self) -> None:
        """robots.txt разрешает раздел, начальная страница открывается и в
        ней есть текст без JavaScript."""
        robots = await self._load_robots()
        url = self._scope.start
        for _ in range(MAX_START_REDIRECTS + 1):
            if not robots.allowed(_request_path(url)):
                raise AdapterConfigError("robots_disallowed")
            response = await self._get(url, max_bytes=PAGE_MAX_BYTES, strict=True)
            if response.is_redirect:
                target = self._scope.normalize(
                    response.headers.get("location", ""), url
                )
                if target is None or not self._scope.in_prefix(target):
                    raise AdapterError("start_page_moved")
                url = target
                continue
            if response.status_code != 200:
                raise _http_error(response.status_code)
            if not _is_html(response):
                raise AdapterError("not_html")
            page = _parse_page(url, response)
            try:
                html_to_markdown(page.html)
            except ExtractionError as exc:
                if exc.code == "no_text":
                    raise AdapterError("site_needs_javascript") from exc
                raise AdapterError(exc.code) from exc
            self._remember(page)
            return
        raise AdapterError("too_many_redirects")

    async def list(self, modules: Sequence[str]) -> AsyncIterator[RemoteDocument]:
        wanted = set(modules) & {MODULE_PAGES, MODULE_FILES}
        if not wanted:
            return
        robots = await self._load_robots()
        entries = await self._sitemap_entries(robots, wanted)
        if entries:
            for url, lastmod in entries:
                if _kind(url) == "file":
                    document = await self._file_document(url, lastmod)
                else:
                    document = await self._sitemap_page(url, lastmod)
                if document is not None:
                    yield document
            return
        async for document in self._crawl(robots, wanted):
            yield document

    async def fetch(
        self, document: RemoteDocument, *, max_bytes: int
    ) -> FetchedContent:
        url = document.locator or document.url
        if not self._scope.owns(url):
            # Адрес пришёл из базы, а не из обхода: на чужой хост не ходим.
            raise AdapterError("url_foreign")
        if document.kind is RemoteDocumentKind.FILE:
            return await self._fetch_file(document, url, max_bytes)
        page = self._cache.pop(url, None)
        if page is None:
            response = await self._get(
                url, max_bytes=min(PAGE_MAX_BYTES, max_bytes), strict=True
            )
            if response.is_redirect:
                raise AdapterError("page_moved")
            if response.status_code != 200:
                raise _http_error(response.status_code)
            if not _is_html(response):
                raise AdapterError("not_html")
            page = _parse_page(url, response)
        if page.noindex:
            raise AdapterError("page_noindex")
        return FetchedPage(html=page.html, title=page.title or None)

    # --- robots.txt и карты ----------------------------------------------------

    async def _load_robots(self) -> RobotsRules:
        if self._robots is not None:
            return self._robots
        try:
            response = await self._client.get(
                f"{self._scope.origin}/robots.txt",
                max_bytes=ROBOTS_FETCH_BYTES,
                follow=True,
            )
        except OutboundTooLargeError as exc:
            raise AdapterError("robots_too_large") from exc
        except AdapterError as exc:
            if exc.retryable:
                raise AdapterError("robots_unavailable", retryable=True) from exc
            raise
        status = response.status_code
        if status == 200:
            text = response.content[:MAX_ROBOTS_BYTES].decode("utf-8", "replace")
            rules = parse_robots(text, ROBOTS_AGENT)
        elif 400 <= status < 500:
            rules = ALLOW_ALL
        else:
            raise AdapterError("robots_unavailable", retryable=True)
        self._client.apply_crawl_delay(rules.crawl_delay)
        self._robots = rules
        return rules

    async def _sitemap_entries(self, robots: RobotsRules, wanted: set[str]) -> _Entries:
        """Адреса раздела из карт сайта, не больше max_pages."""
        origin = self._scope.origin
        candidates = [
            url
            for loc in robots.sitemaps
            if (url := self._scope.normalize(loc, origin)) is not None
        ]
        if not candidates and robots.allowed("/sitemap.xml"):
            candidates = [f"{origin}/sitemap.xml"]
        queue = deque((url, 0) for url in candidates)
        visited: set[str] = set()
        entries: _Entries = []
        seen: set[str] = set()
        while queue and len(visited) < MAX_SITEMAPS:
            sitemap_url, depth = queue.popleft()
            if sitemap_url in visited:
                continue
            visited.add(sitemap_url)
            sitemap = await self._load_sitemap(sitemap_url)
            if sitemap is None:
                continue
            if depth < MAX_SITEMAP_DEPTH:
                for child in sitemap.children:
                    child_url = self._scope.normalize(child, sitemap_url)
                    if child_url is not None:
                        queue.append((child_url, depth + 1))
            for loc, lastmod in sitemap.urls:
                url = self._scope.normalize(loc, sitemap_url)
                if url is None or url in seen or not self._scope.in_prefix(url):
                    continue
                seen.add(url)
                if not robots.allowed(_request_path(url)):
                    continue
                kind = _kind(url)
                if kind == "asset":
                    continue
                if kind == "unsupported":
                    if MODULE_FILES in wanted:
                        note_unsupported(_filename(url), url)
                    continue
                module = MODULE_FILES if kind == "file" else MODULE_PAGES
                if module not in wanted:
                    continue
                entries.append((url, lastmod))
                if len(entries) >= self._max_pages:
                    return entries
        return entries

    async def _load_sitemap(self, url: str) -> Sitemap | None:
        try:
            response = await self._client.get(
                url, max_bytes=MAX_SITEMAP_BYTES, follow=True
            )
        except OutboundTooLargeError:
            logger.info("website_sitemap_skipped", code="sitemap_too_large")
            return None
        if response.status_code != 200:
            return None
        try:
            return parse_sitemap(unpack(response.content))
        except SitemapError as exc:
            logger.info("website_sitemap_skipped", code=exc.code)
            return None

    async def _sitemap_page(
        self, url: str, lastmod: str | None
    ) -> RemoteDocument | None:
        if lastmod:
            return self._page_document(
                url, _title_from_url(url), _short(f"lastmod:{lastmod}")
            )
        response = await self._client.head(url)
        version = _validator(response.headers)
        if response.status_code == 200 and version:
            return self._page_document(url, _title_from_url(url), version)
        if response.status_code not in (200, 405, 501):
            return None
        # Ни lastmod, ни валидаторов (или HEAD не поддержан): скачиваем
        # сейчас; fetch возьмёт страницу из кеша.
        page = await self._load_page(url)
        if page is None or page.noindex:
            return None
        return self._page_document(
            url, page.title or _title_from_url(url), page.version
        )

    # --- обход ссылок -------------------------------------------------------------

    async def _crawl(
        self, robots: RobotsRules, wanted: set[str]
    ) -> AsyncIterator[RemoteDocument]:
        start = self._scope.start
        queue: deque[tuple[str, int]] = deque([(start, 0)])
        queued = {start}
        budget = self._max_pages
        while queue and budget > 0:
            url, depth = queue.popleft()
            if not robots.allowed(_request_path(url)):
                continue
            budget -= 1
            if _kind(url) == "file":
                document = await self._file_document(url, None)
                if document is not None:
                    yield document
                continue
            try:
                response = await self._get(url, max_bytes=PAGE_MAX_BYTES)
            except OutboundTooLargeError:
                note_too_large(url)
                continue
            if response.is_redirect:
                target = self._scope.normalize(
                    response.headers.get("location", ""), url
                )
                if (
                    target is not None
                    and target not in queued
                    and self._scope.in_prefix(target)
                ):
                    queued.add(target)
                    queue.appendleft((target, depth))
                continue
            if response.status_code != 200 or not _is_html(response):
                if response.status_code != 200:
                    logger.info("website_page_skipped", status=response.status_code)
                continue
            page = _parse_page(url, response)
            if not page.nofollow and depth < self._max_depth:
                for link in page.links:
                    target = self._scope.normalize(link, url)
                    if target is None or target in queued:
                        continue
                    queued.add(target)
                    if self._follow(target, robots, wanted):
                        queue.append((target, depth + 1))
            if MODULE_PAGES not in wanted or page.noindex:
                continue
            canonical = (
                self._scope.normalize(page.canonical, url) if page.canonical else None
            )
            if canonical is not None and canonical != url:
                # Дубль: документом будет канонический адрес, если он в разделе.
                if canonical not in queued and self._scope.in_prefix(canonical):
                    queued.add(canonical)
                    queue.append((canonical, depth))
                continue
            self._remember(page)
            yield self._page_document(
                url, page.title or _title_from_url(url), page.version
            )

    def _follow(self, url: str, robots: RobotsRules, wanted: set[str]) -> bool:
        kind = _kind(url)
        if kind == "asset":
            return False
        if kind == "unsupported":
            if MODULE_FILES in wanted:
                note_unsupported(_filename(url), url)
            return False
        if kind == "file":
            # Файл по ссылке со страницы раздела — документ, даже если лежит
            # вне префикса (/upload/…): его опубликовали в этом разделе.
            if MODULE_FILES not in wanted:
                return False
        elif not self._scope.in_prefix(url):
            return False
        return robots.allowed(_request_path(url))

    # --- запросы ------------------------------------------------------------------

    async def _get(
        self, url: str, *, max_bytes: int, strict: bool = False
    ) -> httpx.Response:
        """GET страницы. strict — слишком большая страница — ошибка
        document_too_large (проверка, fetch), иначе исключение наверх."""
        try:
            return await self._client.get(url, max_bytes=max_bytes)
        except OutboundTooLargeError as exc:
            if strict:
                raise AdapterError("document_too_large") from exc
            raise

    async def _load_page(self, url: str) -> _Page | None:
        try:
            response = await self._get(url, max_bytes=PAGE_MAX_BYTES)
        except OutboundTooLargeError:
            note_too_large(url)
            return None
        if response.status_code != 200 or not _is_html(response):
            return None
        page = _parse_page(url, response)
        self._remember(page)
        return page

    def _remember(self, page: _Page) -> None:
        self._cache[page.url] = page
        self._cache.move_to_end(page.url)
        while len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)

    async def _file_document(
        self, url: str, lastmod: str | None
    ) -> RemoteDocument | None:
        size: int | None = None
        if lastmod:
            version = _short(f"lastmod:{lastmod}")
        else:
            response = await self._client.head(url, follow=True)
            status = response.status_code
            if status == 200:
                content_type = response.headers.get("content-type", "").lower()
                if content_type.startswith("text/html"):
                    # Вместо файла — страница (вход, «файл не найден»).
                    return None
                size = to_int(response.headers.get("content-length"))
                if size is not None and size > self._max_bytes:
                    note_too_large(url)
                    return None
                version = _validator(response.headers) or self._day()
            elif status in (405, 501):
                version = self._day()
            else:
                return None
        name = _filename(url)
        return RemoteDocument(
            external_id=_external_id(url),
            title=name,
            url=url,
            version=version,
            kind=RemoteDocumentKind.FILE,
            module=MODULE_FILES,
            path=self._path(url),
            locator=url,
            filename=name,
            size=size,
            visibility=MaterialVisibility.TENANT,
        )

    async def _fetch_file(
        self, document: RemoteDocument, url: str, max_bytes: int
    ) -> FetchedFile:
        if document.size is not None and document.size > max_bytes:
            raise AdapterError("document_too_large")
        downloaded = await self._client.download(url, max_bytes=max_bytes)
        if downloaded.status_code != 200:
            raise _http_error(downloaded.status_code)
        content_type = downloaded.headers.get("content-type", "").lower()
        if content_type.startswith("text/html"):
            raise AdapterError("download_failed")
        return FetchedFile(
            data=downloaded.content, filename=document.filename or _filename(url)
        )

    def _page_document(self, url: str, title: str, version: str) -> RemoteDocument:
        return RemoteDocument(
            external_id=_external_id(url),
            title=title[:200],
            url=url,
            version=version,
            kind=RemoteDocumentKind.PAGE,
            module=MODULE_PAGES,
            path=self._path(url),
            locator=url,
            visibility=MaterialVisibility.TENANT,
        )

    def _path(self, url: str) -> str:
        return f"{self._scope.netloc}{unquote(urlsplit(url).path)}"

    def _day(self) -> str:
        return f"day:{self._today().date().isoformat()}"


def _http_error(status: int) -> AdapterError:
    if status in (404, 410):
        return AdapterError("not_found")
    if status in (401, 403):
        return AdapterError("forbidden")
    return AdapterError(f"http_{status}", retryable=status >= 500)


def build_adapter(
    config: Mapping[str, str],
    http: OutboundClient,
    *,
    max_bytes: int,
    fast: bool = False,
    sleep: Sleep = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    today: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> WebsiteAdapter:
    url = config.get("url", "").strip()
    if not url:
        raise AdapterConfigError("url_missing")
    if code := _check_config(config):
        raise AdapterConfigError(code)
    client = SiteClient(
        http, min_interval=0.0 if fast else MIN_INTERVAL, sleep=sleep, clock=clock
    )
    return WebsiteAdapter(
        client,
        Scope.from_url(url),
        max_pages=_bounded_int(config.get("max_pages"), MAX_PAGES) or DEFAULT_MAX_PAGES,
        max_depth=_bounded_int(config.get("max_depth"), MAX_DEPTH) or DEFAULT_MAX_DEPTH,
        max_bytes=max_bytes,
        today=today,
    )


def register(registry: AdapterRegistry, settings: ConnectorSettings) -> None:
    def factory(
        spec: KindSpec,
        config: Mapping[str, str],
        credentials: Mapping[str, str],
        http: OutboundClient,
        options: AdapterOptions,
    ) -> WebsiteAdapter:
        return build_adapter(
            config, http, max_bytes=settings.max_document_bytes, fast=options.fast
        )

    registry.register(SPEC, factory)
