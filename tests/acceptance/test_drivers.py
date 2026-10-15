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


def test_outline_dex_config_matches_the_manifest() -> None:
    from tests.acceptance.systems.outline import DEX_FILE, body_of, dex_config

    config = dex_config(MANIFEST)

    assert DEX_FILE.read_text(encoding="utf-8") == config
    for employee in MANIFEST.employees.values():
        assert f"email: {employee.email}" in config
    assert "https://127.0.0.1:8542/auth/oidc.callback" in config
    # Заголовок страницы Outline берёт из title, а не из текста.
    assert body_of("# Название\n\nШифр документа: SV-X-001\n") == (
        "Шифр документа: SV-X-001\n"
    )


def test_seafile_groups_are_named_by_zone() -> None:
    from tests.acceptance.systems.seafile import group_name

    assert group_name("payroll") == "acc-payroll"


def test_confluence_storage_format_and_names() -> None:
    from tests.acceptance.systems.confluence_dc import (
        group_name,
        space_key,
        to_storage,
    )

    text = to_storage(
        "# Заголовок\n\n- пункт <1>\n- пункт 2\n1. шаг\n\n**Жирный** & текст\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n"
    )

    assert "<h1>Заголовок</h1>" in text
    assert "<ul>\n<li>пункт &lt;1&gt;</li>\n<li>пункт 2</li>\n</ul>" in text
    assert "<ol>\n<li>шаг</li>\n</ol>" in text
    assert "<p><strong>Жирный</strong> &amp; текст</p>" in text
    assert "<tr><th>a</th><th>b</th></tr>" in text
    assert "<tr><td>1</td><td>2</td></tr>" in text
    assert space_key("payroll") == "PAYROLL"
    assert group_name("payroll") == "acc-payroll"


def test_atlassian_systems_fit_the_ten_user_license() -> None:
    """Тестовая лицензия — 10 пользователей: администратор плюс сотрудники."""
    for name in ("confluence_dc", "jira_dc"):
        accounts = MANIFEST.systems[name].accounts
        assert len(accounts) <= 9, name
        # У каждой закрытой зоны есть и читатели, и не читатели.
        for zone in MANIFEST.zones.values():
            if zone.closed:
                readers = MANIFEST.readers_of(zone.id) & set(accounts)
                assert readers and set(accounts) - readers, (name, zone.id)


def test_jira_markup_and_project_keys() -> None:
    from tests.acceptance.systems.jira_dc import (
        group_name,
        project_key,
        to_jira_markup,
    )

    text = to_jira_markup(
        "# Заголовок\n\n## Раздел\n\n- пункт\n1. шаг\n\n**Жирный** текст\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n"
    )

    assert "h1. Заголовок" in text and "h2. Раздел" in text
    assert "* пункт" in text and "# шаг" in text
    assert "*Жирный* текст" in text
    assert "||a||b||" in text and "|1|2|" in text
    assert group_name("payroll") == "acc-payroll"
    # Ключи проектов не совпадают с буквами шифров документов: Jira
    # превращает «HR-001» в ссылку на задачу и шифр пропадает из текста.
    codes = {d.id.split("-")[1] for d in MANIFEST.documents.values()}
    keys = {project_key(zone) for zone in MANIFEST.zones}
    assert not keys & codes
    assert all(2 <= len(key) <= 10 for key in keys)
