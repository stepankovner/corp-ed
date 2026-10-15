"""Поля-списки форм подключений (FieldSpec.multiline): разбор принимает и
переводы строк (в том числе из Windows), и прежние разделители."""

import pytest

from corp_ed.connectors.amocrm.adapter import _pipelines
from corp_ed.connectors.common import split_list
from corp_ed.connectors.dokuwiki.adapter import parse_users
from corp_ed.connectors.elma365.adapter import _folders as elma_folders
from corp_ed.connectors.jira_dc.adapter import _project_keys
from corp_ed.connectors.m365.adapter import _site_urls
from corp_ed.connectors.mediawiki.rights import parse_user_emails
from corp_ed.connectors.r7office.adapter import _folder_ids
from corp_ed.connectors.registry import default_registry
from corp_ed.connectors.webdav.kinds import parse_folders
from corp_ed.connectors.yandex.tracker import parse_queues
from corp_ed.connectors.yandex.wiki import parse_roots
from corp_ed.core.config import ConnectorSettings

MULTILINE_FIELDS = {
    "dokuwiki": {"users"},
    "mediawiki": {"user_emails"},
    "r7office": {"folders"},
    "elma365": {"folders"},
    "jira_dc": {"projects"},
    "confluence": {"spaces"},
    "kaiten": {"spaces"},
    "m365": {"sites"},
    "amocrm": {"pipelines"},
    "yandex360": {"wiki_roots", "tracker_queues"},
    # Папки — общий FieldSpec видов на WebDAV.
    "webdav": {"folders"},
    "nextcloud": {"folders"},
    "nextcloud_oauth": {"folders"},
    "owncloud": {"folders"},
    "seafile": {"folders"},
    "mailru_cloud": {"folders"},
    "vk_workspace_disk": {"folders"},
}


def test_multiline_flag_is_set_on_list_fields_only() -> None:
    registry = default_registry(ConnectorSettings())
    found: dict[str, set[str]] = {}
    for kind, spec in registry._specs.items():
        credentials = spec.credential_fields + spec.app_credential_fields
        assert not any(f.multiline for f in credentials), kind
        if names := {f.name for f in spec.config_fields if f.multiline}:
            found[kind] = names
    assert found == MULTILINE_FIELDS


@pytest.mark.parametrize(
    "value",
    ["a,b,c", "a\nb\nc", "a\r\nb\r\nc\r\n", "a, b\n\nc,", " a ,\n b \n c "],
)
def test_split_list_accepts_lines_and_commas(value: str) -> None:
    assert split_list(value) == ["a", "b", "c"]
    assert split_list(None) == [] and split_list(" \n,") == []


@pytest.mark.parametrize("sep", [",", "\n", "\r\n", ", "])
def test_list_parsers_accept_every_separator(sep: str) -> None:
    ids = sep.join(["10", "20"])
    assert _folder_ids({"folders": ids}) == ["10", "20"]
    assert _pipelines({"pipelines": ids}) == ["10", "20"]
    assert _project_keys({"projects": sep.join(["HR", "OPS"])}) == ["HR", "OPS"]
    assert parse_queues(sep.join(["hr", "ops"])) == ("HR", "OPS")
    assert parse_roots(sep.join(["hr", "ops"])) == ("hr", "ops")
    assert parse_folders(sep.join(["Документы", "Общие/Регламенты"])) == [
        ("Документы",),
        ("Общие", "Регламенты"),
    ]
    uuid = "0" * 8 + "-" + "0" * 4 + "-" + "0" * 4 + "-" + "0" * 4 + "-" + "0" * 12
    assert elma_folders({"folders": sep.join([uuid, uuid.upper()])}) == [uuid, uuid]


def test_site_urls_split_on_lines() -> None:
    urls = "https://a.sharepoint.com/sites/hr\r\nhttps://a.sharepoint.com/sites/it"
    assert _site_urls(urls) == [
        "https://a.sharepoint.com/sites/hr",
        "https://a.sharepoint.com/sites/it",
    ]


def test_dokuwiki_users_by_lines_and_semicolons() -> None:
    expected = {"ivanov": "ivanov@example.com", "petrov": "petrov@example.com"}
    for value in (
        "ivanov ivanov@example.com\npetrov petrov@example.com",
        "ivanov ivanov@example.com\r\npetrov petrov@example.com\r\n",
        "ivanov ivanov@example.com; petrov=petrov@example.com",
    ):
        assert parse_users(value) == expected


def test_mediawiki_user_emails_by_lines_and_commas() -> None:
    expected = {"Ivanov": "ivanov@example.com", "Petrov": "petrov@example.com"}
    for value in (
        "Ivanov = ivanov@example.com\nPetrov = petrov@example.com",
        "Ivanov = ivanov@example.com\r\nPetrov = petrov@example.com\r\n",
        "Ivanov = ivanov@example.com, Petrov = petrov@example.com",
    ):
        assert parse_user_emails(value) == expected
