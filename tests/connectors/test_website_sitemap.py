"""Разбор sitemap.xml: набор адресов, индекс карт, gzip, враждебный XML."""

import gzip

import pytest

from corp_ed.connectors.website.sitemap import (
    SitemapError,
    parse_sitemap,
    unpack,
)

URLSET = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.example.ru/help/</loc><lastmod>2026-09-01</lastmod></url>
  <url><loc> https://www.example.ru/help/delivery </loc></url>
  <url><lastmod>2026-09-01</lastmod></url>
</urlset>"""

INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://www.example.ru/sitemap-1.xml</loc></sitemap>
  <sitemap><loc>https://www.example.ru/sitemap-2.xml.gz</loc></sitemap>
</sitemapindex>"""


def test_urlset_with_lastmod() -> None:
    parsed = parse_sitemap(URLSET)
    assert parsed.children == ()
    assert parsed.urls == (
        ("https://www.example.ru/help/", "2026-09-01"),
        ("https://www.example.ru/help/delivery", None),
    )


def test_index_lists_child_sitemaps() -> None:
    parsed = parse_sitemap(INDEX)
    assert parsed.urls == ()
    assert parsed.children == (
        "https://www.example.ru/sitemap-1.xml",
        "https://www.example.ru/sitemap-2.xml.gz",
    )


def test_without_namespace() -> None:
    parsed = parse_sitemap(b"<urlset><url><loc>https://a.ru/x</loc></url></urlset>")
    assert parsed.urls == (("https://a.ru/x", None),)


def test_gzip_is_unpacked_with_a_limit() -> None:
    assert unpack(gzip.compress(URLSET), limit=10_000) == URLSET
    assert unpack(URLSET, limit=10_000) == URLSET
    bomb = gzip.compress(b"<" + b" " * 2_000_000 + b">")
    with pytest.raises(SitemapError, match="sitemap_too_large"):
        unpack(bomb, limit=1_000_000)
    with pytest.raises(SitemapError, match="sitemap_invalid"):
        unpack(b"\x1f\x8bnot-gzip", limit=1000)


@pytest.mark.parametrize(
    "data",
    [
        b"not xml at all",
        b"<html><body>404</body></html>",
        b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>
<urlset><url><loc>&lol2;</loc></url></urlset>""",
        b"""<?xml version="1.0"?>
<!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<urlset><url><loc>&xxe;</loc></url></urlset>""",
    ],
)
def test_hostile_or_foreign_documents_are_rejected(data: bytes) -> None:
    with pytest.raises(SitemapError, match="sitemap_invalid"):
        parse_sitemap(data)
