"""cli connector-check: адаптер против источника без базы, запись фикстур."""

import argparse
import json
from pathlib import Path

import pytest

from corp_ed import cli
from tests.connectors.fake_portal import FakePortal, sample_portal

# Литерал публичного адреса: проверка адреса в CLI идёт через системный
# резолвер, а DNS в тестах нет.
HOST = "93.184.216.34"


@pytest.fixture
def portal() -> FakePortal:
    return sample_portal(host=HOST)


@pytest.fixture(autouse=True)
def preview_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    """connector-check — инструмент живой проверки: непроверенные модули
    включаются тем же флагом, что и в продукте."""
    monkeypatch.setenv("CONNECTOR_PREVIEW_MODULES", "knowledge_base_v2")


def args(
    portal: FakePortal, record: Path | None = None, **overrides: object
) -> argparse.Namespace:
    values: dict[str, object] = {
        "kind": "bitrix24",
        "config": [f"portal={portal.portal}"],
        "credential": [f"webhook={portal.webhook}"],
        "module": ["disk", "knowledge_base", "knowledge_base_v2"],
        "limit": 50,
        "fetch": 1,
        "record": str(record) if record else None,
        "fast": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


async def test_check_lists_and_fetches(
    portal: FakePortal, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    record = tmp_path / "rec"
    code = await cli._connector_check(args(portal, record), http=portal.client())
    out = capsys.readouterr().out
    assert code == 0, out
    assert "check: ok" in out
    assert "[disk] disk:102 «Отпуск.txt»" in out
    assert "[knowledge_base] kb:KNOWLEDGE:985 «Отпуск»" in out
    assert "скачано disk:102: txt" in out
    assert "скачано kb:KNOWLEDGE:985: html" in out
    assert "[knowledge_base_v2] note:10 «Введение»" in out
    assert "скачано note:10: md" in out
    files = sorted(record.glob("*.json"))
    assert files and files[0].name == "001-profile.json"
    dumped = "\n".join(f.read_text(encoding="utf-8") for f in files)
    assert "wh-code" not in dumped
    assert "signed-1" not in dumped
    recorded = json.loads(files[0].read_text(encoding="utf-8"))
    assert recorded["method"] == "profile"
    assert recorded["response"]["result"]["ID"] == "1"


async def test_check_reports_rejected_credentials(
    portal: FakePortal, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = args(portal, credential=[f"webhook={portal.portal}rest/1/wrong/"])
    code = await cli._connector_check(bad, http=portal.client())
    assert code == 1
    assert "check: ОШИБКА invalid_credentials" in capsys.readouterr().out


async def test_check_rejects_unknown_kind_and_module(
    portal: FakePortal, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        await cli._connector_check(args(portal, kind="nope"), http=portal.client()) == 2
    )
    assert (
        await cli._connector_check(args(portal, module=["mail"]), http=portal.client())
        == 2
    )
    err = capsys.readouterr().err
    assert "неизвестный вид" in err and "неизвестные модули" in err


async def test_check_rejects_private_portal_address(
    portal: FakePortal, capsys: pytest.CaptureFixture[str]
) -> None:
    private = args(portal, config=["portal=https://10.0.0.5/"])
    assert await cli._connector_check(private, http=portal.client()) == 2
    assert "address_not_public" in capsys.readouterr().err


def test_cli_help_mentions_connector_check() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["connector-check", "--help"])
    assert excinfo.value.code == 0


async def test_check_records_confluence_and_yandex_too(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """--record и --fast действуют для любого вида, не только Битрикс24."""
    from tests.connectors.fake_confluence import PAT, sample_confluence
    from tests.connectors.fake_yandex import (
        ACCESS_TOKEN,
        CLIENT_ID,
        CLIENT_SECRET,
        REFRESH_TOKEN,
        sample_yandex,
    )

    confluence = sample_confluence()
    confluence.host = HOST
    record = tmp_path / "confluence"
    args_confluence = argparse.Namespace(
        kind="confluence",
        config=[f"base_url={confluence.base}", "spaces=HR"],
        credential=[f"token={PAT}"],
        module=["pages"],
        limit=50,
        fetch=1,
        record=str(record),
        fast=True,
    )
    code = await cli._connector_check(args_confluence, http=confluence.client())
    out = capsys.readouterr().out
    assert code == 0, out
    assert "[pages] page:100 «Отпуск»" in out
    files = sorted(record.glob("*.json"))
    assert files and files[0].name == "001-user_current.json"
    dumped = "\n".join(f.read_text(encoding="utf-8") for f in files)
    assert PAT not in dumped

    yandex = sample_yandex()
    record_y = tmp_path / "yandex"
    args_yandex = argparse.Namespace(
        kind="yandex360",
        config=[f"client_id={CLIENT_ID}"],
        credential=[
            f"client_secret={CLIENT_SECRET}",
            f"access_token={ACCESS_TOKEN}",
            f"refresh_token={REFRESH_TOKEN}",
        ],
        module=["disk"],
        limit=50,
        fetch=1,
        record=str(record_y),
        fast=True,
    )
    code = await cli._connector_check(args_yandex, http=yandex.client())
    out = capsys.readouterr().out
    assert code == 0, out
    assert "[disk] ydisk:rid-disk:/Регламенты/Отпуск.txt" in out
    dumped = "\n".join(f.read_text(encoding="utf-8") for f in record_y.glob("*.json"))
    assert dumped and ACCESS_TOKEN not in dumped and CLIENT_SECRET not in dumped


async def test_preview_module_needs_the_flag(
    portal: FakePortal,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONNECTOR_PREVIEW_MODULES")

    code = await cli._connector_check(args(portal), http=portal.client())

    assert code == 2
    assert "неизвестные модули knowledge_base_v2" in capsys.readouterr().err


async def test_check_runs_google_drive_without_leaking_the_key(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Живая проверка Google: ключ — одним аргументом, в записанных
    ответах нет ни закрытого ключа, ни токенов."""
    from tests.connectors.fake_google import (
        ADMIN,
        DOMAIN,
        private_key_pem,
        sample_google,
        service_account_key,
    )

    google = sample_google()
    # Скачивается первый документ каждого модуля, разбор — настоящий:
    # поддельные docx и pdf его не пройдут, текстовый — пройдёт.
    for file_id in ("f-plan", "f-budget", "f-report"):
        google.files.pop(file_id)
    record = tmp_path / "gdrive"
    namespace = argparse.Namespace(
        kind="gdrive",
        config=[f"domains={DOMAIN}", f"admin_email={ADMIN}"],
        credential=[f"service_account_key={service_account_key()}"],
        module=["shared_drives", "user_drives"],
        limit=50,
        fetch=1,
        record=str(record),
        fast=True,
    )
    code = await cli._connector_check(namespace, http=google.client())
    out = capsys.readouterr().out
    assert code == 0, out
    assert "[shared_drives] gdrive:f-vacation «Отпуск.txt»" in out
    assert "[user_drives] gdrive:f-partner «Для партнёра.txt»" in out
    dumped = "\n".join(f.read_text(encoding="utf-8") for f in record.glob("*.json"))
    assert dumped
    assert private_key_pem().splitlines()[1] not in dumped
    assert "ya29." not in dumped
