"""Чистые части драйверов засева (без Docker): разметка, имена, настройки.

Сами драйверы проверяются на стендах (`python -m tests.acceptance.run
--system <вид> --seed`); здесь — то, что можно проверить без сервера.
"""

from tests.acceptance.model import load_manifest
from tests.acceptance.systems.mediawiki import (
    group_name as mw_group,
)
from tests.acceptance.systems.mediawiki import (
    namespace_name,
    page_title,
    settings_php,
    to_wikitext,
    wiki_user,
)

MANIFEST = load_manifest()


def test_mediawiki_wikitext_covers_headings_lists_tables_and_bold() -> None:
    text = to_wikitext(
        "# Заголовок\n\n## Раздел\n\n- пункт\n1. шаг\n\n**Жирный** текст\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n"
    )

    assert "= Заголовок =" in text
    assert "== Раздел ==" in text
    assert "* пункт" in text and "# шаг" in text
    assert "'''Жирный''' текст" in text
    assert '{| class="wikitable"' in text
    assert "! a !! b" in text and "| 1 || 2" in text and text.count("|}") == 1


def test_mediawiki_names_and_lockdown_follow_the_zones() -> None:
    zones = list(MANIFEST.zones)
    php = settings_php(zones)

    assert page_title("hr", "SV-HR-005") == "Zone_hr:SV-HR-005"
    assert wiki_user("vetrov") == "Vetrov"
    # Открытая зона — право read на всю вики (группа staff), без Lockdown.
    assert mw_group("public") == "staff"
    assert "$wgExtraNamespaces[3100] = 'Zone_public';" in php
    assert "$wgNamespacePermissionLockdown[3100]" not in php
    # У каждой закрытой зоны — свой номер и своя группа.
    for zone in zones[1:]:
        assert f"'{namespace_name(zone)}'" in php
        assert f"['{mw_group(zone)}', 'sysop']" in php
    assert php.count("$wgNamespacePermissionLockdown") == len(zones) - 1


def test_mediawiki_takes_only_the_open_zone() -> None:
    system = MANIFEST.systems["mediawiki"]

    assert system.synced_zones == ("public",)
    assert {MANIFEST.documents[d].zone for d in system.docs} - {"public"}
