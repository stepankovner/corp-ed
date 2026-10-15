"""Вспомогательное в приёмке: учётные данные, разметка DokuWiki, таблица."""

import json
from pathlib import Path

from tests.acceptance.model import load_manifest
from tests.acceptance.oauth_login import extract_code
from tests.acceptance.run import format_table
from tests.acceptance.scenario import OK, SKIP, StepResult
from tests.acceptance.systems import make_driver, resolve_credentials
from tests.acceptance.systems.base import (
    Creds,
    env_credentials,
    merge,
    read_credentials,
    write_credentials,
)
from tests.acceptance.systems.dokuwiki import group_name, page_id, to_dokuwiki

MANIFEST = load_manifest()


def test_env_credentials_read_connection_and_each_employee() -> None:
    env = {
        "ACC_KAITEN_BASE_URL": "https://krontoai.kaiten.ru",
        "ACC_KAITEN_VETROV_TOKEN": "t1",
        "ACC_KAITEN_BELOVA_TOKEN": "t2",
        "ACC_OTHER_VETROV_TOKEN": "x",
    }
    creds = env_credentials(
        "kaiten", ["base_url"], [], [], ["token"], ["vetrov", "belova", "orlov"], env
    )

    assert creds is not None
    assert creds.config == {"base_url": "https://krontoai.kaiten.ru"}
    assert creds.users == {"vetrov": {"token": "t1"}, "belova": {"token": "t2"}}
    assert env_credentials("yonote", ["base_url"], [], [], [], ["vetrov"], env) is None


def test_environment_overrides_the_file_field_by_field(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    write_credentials(
        path,
        "kaiten",
        Creds(config={"base_url": "https://a"}, users={"vetrov": {"token": "old"}}),
    )
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    from_file = read_credentials(path, "kaiten")
    over = Creds(users={"vetrov": {"token": "new"}, "belova": {"token": "b"}})

    merged = merge(from_file, over)

    assert merged is not None
    assert merged.config == {"base_url": "https://a"}
    assert merged.users["vetrov"] == {"token": "new"}
    assert merged.users["belova"] == {"token": "b"}
    assert read_credentials(path, "missing") is None
    assert json.loads(path.read_text(encoding="utf-8"))["kaiten"]["config"]


def test_system_without_credentials_is_reported_unavailable() -> None:
    system = MANIFEST.systems["kaiten"]
    driver = make_driver(system, MANIFEST, None)
    assert driver.unavailable() is not None

    ready = make_driver(
        system,
        MANIFEST,
        Creds(
            config={"base_url": "https://krontoai.kaiten.ru"},
            users={"vetrov": {"token": "t"}},
        ),
    )
    assert ready.unavailable() is None
    # Yonote: адрес не обязателен (облако по умолчанию), ключ — обязателен.
    yonote = MANIFEST.systems["yonote"]
    assert (
        make_driver(yonote, MANIFEST, Creds(organization={"token": "k"})).unavailable()
        is None
    )


def test_resolve_credentials_prefers_environment(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "c.json"
    write_credentials(
        path,
        "yonote",
        Creds(config={"base_url": "https://a"}, organization={"token": "f"}),
    )
    monkeypatch.setenv("ACC_YONOTE_TOKEN", "from-env")

    creds = resolve_credentials(MANIFEST.systems["yonote"], MANIFEST, path)

    assert creds is not None
    assert creds.organization == {"token": "from-env"}
    assert creds.config == {"base_url": "https://a"}


def test_oauth_code_is_taken_from_a_pasted_address() -> None:
    assert extract_code("abc123") == "abc123"
    assert extract_code("https://stage.krontoai.ru/cb?code=xyz&state=s") == "xyz"


def test_dokuwiki_markup_and_names() -> None:
    text = to_dokuwiki(
        "# Заголовок\n\n## Раздел\n\n- пункт\n1. шаг\n\n"
        "| a | b |\n|---|---|\n| 1 | 2 |\n"
    )

    assert "====== Заголовок ======" in text
    assert "===== Раздел =====" in text
    assert "  * пункт" in text and "  - шаг" in text
    assert "^ a ^ b ^" in text and "| 1 | 2 |" in text
    assert page_id("sales", "SV-SAL-001") == "sales:sv-sal-001"
    # Подчёркивание в имени группы ломает правило ACL (кодируется как %5f).
    assert "_" not in group_name("payroll")


def test_table_has_a_column_per_step() -> None:
    systems = [MANIFEST.systems["bookstack"], MANIFEST.systems["kaiten"]]
    results = [StepResult("bookstack", "sync", OK), StepResult("kaiten", "sync", SKIP)]

    table = format_table(MANIFEST, results, systems)

    head = table.splitlines()[0].split()
    assert head == ["система", "sync", "resync", "edit", "delete", "revoke", "move"]
    assert table.splitlines()[2].split()[:2] == ["bookstack", "ok"]
    assert table.splitlines()[3].split()[:3] == ["kaiten", "skip", "-"]
