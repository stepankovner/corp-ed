"""robots.txt по RFC 9309: группа нашего агента или *, самое длинное
совпадение, Allow при равенстве, шаблоны * и $, Crawl-delay, Sitemap."""

import pytest

from corp_ed.connectors.website.robots import ALLOW_ALL, RobotsRules, parse_robots

ROBOTS = """
# комментарий
User-agent: *
Disallow: /admin/
Disallow: /search
Allow: /admin/public/
Crawl-delay: 2

User-agent: GoogleBot
Disallow: /

Sitemap: https://www.example.ru/sitemap.xml
Sitemap: https://www.example.ru/sitemap-news.xml.gz
"""


def test_star_group_applies_to_us() -> None:
    rules = parse_robots(ROBOTS, "kronto-bot")
    assert rules.allowed("/help/")
    assert not rules.allowed("/admin/users")
    assert rules.allowed("/admin/public/page")
    assert not rules.allowed("/search?q=1")
    assert not rules.allowed("/searching")
    assert rules.crawl_delay == 2.0
    assert rules.sitemaps == (
        "https://www.example.ru/sitemap.xml",
        "https://www.example.ru/sitemap-news.xml.gz",
    )


def test_own_group_wins_over_star_case_insensitive() -> None:
    text = ROBOTS + "\nUser-agent: Kronto-Bot\nDisallow: /help/private\n"
    rules = parse_robots(text, "kronto-bot")
    # Своя группа заменяет *: /admin/ ей не запрещён.
    assert rules.allowed("/admin/users")
    assert not rules.allowed("/help/private/1")
    assert rules.crawl_delay is None


def test_consecutive_user_agents_share_a_group() -> None:
    text = "User-agent: other\nUser-agent: kronto-bot\nDisallow: /x\n"
    rules = parse_robots(text, "kronto-bot")
    assert not rules.allowed("/x/1")


def test_longest_match_and_allow_wins_ties() -> None:
    text = "User-agent: *\nDisallow: /docs\nAllow: /docs\nDisallow: /a/b\nAllow: /a\n"
    rules = parse_robots(text, "kronto-bot")
    assert rules.allowed("/docs/1")
    assert not rules.allowed("/a/b/c")
    assert rules.allowed("/a/c")


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("/file.pdf", False),
        ("/file.pdf?x=1", True),
        ("/dir/file.PDF", True),
        ("/private-1/page", False),
        ("/public/page", True),
    ],
)
def test_wildcards(path: str, allowed: bool) -> None:
    text = "User-agent: *\nDisallow: /*.pdf$\nDisallow: /private*/\n"
    assert parse_robots(text, "kronto-bot").allowed(path) is allowed


def test_empty_disallow_allows_everything() -> None:
    rules = parse_robots("User-agent: *\nDisallow:\n", "kronto-bot")
    assert rules.allowed("/anything")


def test_disallow_all_and_percent_encoding() -> None:
    rules = parse_robots("User-agent: *\nDisallow: /\nAllow: /%D0%B0\n", "kronto-bot")
    assert not rules.allowed("/")
    assert rules.allowed("/%D0%B0/page")
    assert rules.allowed("/а/page")


def test_no_group_and_allow_all() -> None:
    rules = parse_robots("User-agent: other\nDisallow: /\n", "kronto-bot")
    assert rules.allowed("/")
    assert ALLOW_ALL.allowed("/admin") and ALLOW_ALL.sitemaps == ()


def test_garbage_and_bad_crawl_delay_are_ignored() -> None:
    text = "\x00junk\nUser-agent: *\nCrawl-delay: soon\nDisallow /no-colon\n"
    rules = parse_robots(text, "kronto-bot")
    assert isinstance(rules, RobotsRules)
    assert rules.crawl_delay is None
    assert rules.allowed("/no-colon")
