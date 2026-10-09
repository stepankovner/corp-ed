"""Карта сайта (sitemaps.org 0.9): адреса страниц с lastmod и индексы карт.

XML пришёл с чужого сайта — разбирается через defusedxml с запретом DTD:
ни сущностей, ни внешних ссылок (billion laughs, XXE). Сжатая карта (.xml.gz
или тело, начинающееся с сигнатуры gzip) распаковывается не больше лимита:
протокол разрешает до 50 МиБ несжатого XML, больше — карта отвергается,
а не раздувается в памяти воркера. Сетевых загрузок нет.
"""

import zlib
from dataclasses import dataclass
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException  # type: ignore[import-untyped]
from defusedxml.ElementTree import fromstring  # type: ignore[import-untyped]

MAX_SITEMAP_BYTES = 50 * 1024 * 1024
_GZIP_MAGIC = b"\x1f\x8b"


class SitemapError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Sitemap:
    urls: tuple[tuple[str, str | None], ...] = ()
    """(адрес, lastmod) страниц из <urlset>."""
    children: tuple[str, ...] = ()
    """Адреса вложенных карт из <sitemapindex>."""


def unpack(data: bytes, *, limit: int = MAX_SITEMAP_BYTES) -> bytes:
    """Распаковать gzip, если это gzip; не больше limit байт."""
    if not data.startswith(_GZIP_MAGIC):
        return data
    inflater = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
    try:
        out = inflater.decompress(data, limit + 1)
    except zlib.error as exc:
        raise SitemapError("sitemap_invalid") from exc
    if len(out) > limit or inflater.unconsumed_tail:
        raise SitemapError("sitemap_too_large")
    return out


def parse_sitemap(data: bytes) -> Sitemap:
    try:
        # Картам сайта DTD не нужен: запрещён целиком, как в ingest/ooxml.py.
        root = fromstring(data, forbid_dtd=True)
    except (ParseError, DefusedXmlException, ValueError) as exc:
        raise SitemapError("sitemap_invalid") from exc
    kind = _local(root)
    if kind == "urlset":
        urls: list[tuple[str, str | None]] = []
        for item in root:
            if _local(item) != "url":
                continue
            loc = _child_text(item, "loc")
            if loc:
                urls.append((loc, _child_text(item, "lastmod")))
        return Sitemap(urls=tuple(urls))
    if kind == "sitemapindex":
        children = [
            loc
            for item in root
            if _local(item) == "sitemap" and (loc := _child_text(item, "loc"))
        ]
        return Sitemap(children=tuple(children))
    raise SitemapError("sitemap_invalid")


def _local(element: Element) -> str:
    tag = element.tag if isinstance(element.tag, str) else ""
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(element: Element, name: str) -> str | None:
    for child in element:
        if _local(child) == name:
            text = (child.text or "").strip()
            return text or None
    return None
