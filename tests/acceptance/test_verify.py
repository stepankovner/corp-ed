"""Локальная сверка выгрузки connector-visibility с манифестом (verify.py)."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from tests.acceptance.model import Expected, load_manifest
from tests.acceptance.verify import main, verify

MANIFEST = load_manifest()
SYSTEM = MANIFEST.systems["yandex360"]
DOMAIN = "krontoai.ru"
CONNECTOR_ID = "11111111-1111-1111-1111-111111111111"


def good_report(**overrides: Any) -> dict[str, Any]:
    """Выгрузка, в точности совпадающая с манифестом."""
    expected = Expected(MANIFEST, SYSTEM)
    docs = [s.document.id for s in expected.present_docs()]
    connector = {
        "id": CONNECTOR_ID,
        "kind": SYSTEM.kind,
        "name": "Яндекс 360",
        "materials": len(docs),
        "documents": {doc: 1 for doc in sorted(docs)},
        "without_code": 0,
        "seen": {
            f"{login}@{DOMAIN}": sorted(expected.visible_to(login))
            for login in SYSTEM.accounts
        },
    }
    connector.update(overrides)
    return {"company": "demo", "connectors": [connector]}


def problems_of(report: dict[str, Any], **options: Any) -> list[str]:
    return verify(MANIFEST, SYSTEM, report, domain=DOMAIN, **options).problems


def test_matching_report_is_ok() -> None:
    verdict = verify(MANIFEST, SYSTEM, good_report(), domain=DOMAIN)
    assert verdict.ok, verdict.problems
    assert verdict.warnings == []


def test_the_manifest_really_hides_something_from_somebody() -> None:
    # Без этого «совпало» ничего бы не доказывало.
    expected = Expected(MANIFEST, SYSTEM)
    assert any(expected.hidden_from(login) for login in SYSTEM.accounts)


def test_extra_visible_document_is_a_leak() -> None:
    expected = Expected(MANIFEST, SYSTEM)
    login = next(x for x in SYSTEM.accounts if expected.hidden_from(x))
    hidden = sorted(expected.hidden_from(login))[0]
    report = good_report()
    report["connectors"][0]["seen"][f"{login}@{DOMAIN}"].append(hidden)

    assert problems_of(report) == [f"УТЕЧКА: {login} видит {hidden}, а не должен"]


def test_missing_visible_document_is_a_difference() -> None:
    expected = Expected(MANIFEST, SYSTEM)
    login = next(x for x in SYSTEM.accounts if expected.visible_to(x))
    lost = sorted(expected.visible_to(login))[0]
    report = good_report()
    report["connectors"][0]["seen"][f"{login}@{DOMAIN}"].remove(lost)

    assert problems_of(report) == [f"{login} не видит {lost}, а должен"]


def test_document_missing_in_kronto_and_unknown_document() -> None:
    report = good_report()
    documents = report["connectors"][0]["documents"]
    gone = sorted(documents)[0]
    del documents[gone]
    documents["SV-XXX-999"] = 1

    problems = problems_of(report)

    assert any(p.startswith(f"нет документа {gone}") for p in problems)
    assert "лишний документ SV-XXX-999" in problems


def test_employee_absent_from_report_is_a_difference_and_stranger_is_ignored() -> None:
    report = good_report()
    seen = report["connectors"][0]["seen"]
    del seen[f"{SYSTEM.accounts[0]}@{DOMAIN}"]
    seen[f"admin@{DOMAIN}"] = ["SV-HR-001"]
    seen["someone@other.example"] = []

    verdict = verify(MANIFEST, SYSTEM, report, domain=DOMAIN)

    assert verdict.problems == [
        f"{SYSTEM.accounts[0]}@{DOMAIN}: нет в выгрузке (не активный участник "
        "компании или другая почта)"
    ]
    assert len(verdict.warnings) == 2
    assert any(w.startswith(f"admin@{DOMAIN}") for w in verdict.warnings)


def test_emails_are_matched_case_insensitively_and_domain_is_a_parameter() -> None:
    report = good_report()
    seen = report["connectors"][0]["seen"]
    report["connectors"][0]["seen"] = {
        email.upper().replace(DOMAIN.upper(), "Stand.Example"): codes
        for email, codes in seen.items()
    }

    assert not verify(MANIFEST, SYSTEM, report, domain=DOMAIN).ok  # чужой домен
    assert verify(MANIFEST, SYSTEM, report, domain="stand.example").ok


def test_duplicate_copies_do_not_matter() -> None:
    # у per_user один документ лежит копиями: счётчик 3 — всё ещё один документ
    report = good_report()
    documents = report["connectors"][0]["documents"]
    documents[sorted(documents)[0]] = 3

    assert problems_of(report) == []


def test_connector_choice() -> None:
    first = good_report()["connectors"][0]
    second = copy.deepcopy(first) | {"id": "22222222-2222-2222-2222-222222222222"}
    second["seen"][f"{SYSTEM.accounts[0]}@{DOMAIN}"] = []
    report = {"company": "demo", "connectors": [first, second]}

    with pytest.raises(ValueError, match="несколько"):
        verify(MANIFEST, SYSTEM, report, domain=DOMAIN)
    assert verify(MANIFEST, SYSTEM, report, connector_id=CONNECTOR_ID, domain=DOMAIN).ok
    assert not verify(
        MANIFEST, SYSTEM, report, connector_id=second["id"], domain=DOMAIN
    ).ok
    with pytest.raises(ValueError, match="нет подключения"):
        verify(MANIFEST, SYSTEM, report, connector_id="nope", domain=DOMAIN)


def run_main(tmp_path: Path, report: dict[str, Any]) -> int:
    path = tmp_path / "visibility.json"
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return main(["--system", "yandex360", "--observed", str(path)])


def test_command_line_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_main(tmp_path, good_report()) == 0
    assert capsys.readouterr().out.strip() == "ok"

    expected = Expected(MANIFEST, SYSTEM)
    login = next(x for x in SYSTEM.accounts if expected.hidden_from(x))
    hidden = sorted(expected.hidden_from(login))[0]
    report = good_report()
    report["connectors"][0]["seen"][f"{login}@{DOMAIN}"].append(hidden)
    report["connectors"][0]["seen"][f"admin@{DOMAIN}"] = []
    assert run_main(tmp_path, report) == 1
    captured = capsys.readouterr()
    assert f"УТЕЧКА: {login} видит {hidden}" in captured.out
    assert f"admin@{DOMAIN}" in captured.err

    assert run_main(tmp_path, {"company": "demo"}) == 2
    assert "выгрузка не разобрана" in capsys.readouterr().err

    assert main(["--system", "no-such", "--observed", str(tmp_path)]) == 2
    assert "нет системы" in capsys.readouterr().err
