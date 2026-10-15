"""Манифест приёмки: целостность, вымышленность данных и логика ожиданий.

Без базы и без сети — идёт в обычном прогоне pytest.
"""

import re
from pathlib import Path

import pytest

from tests.acceptance.corpus.build import CORPUS, read_source
from tests.acceptance.model import (
    Expected,
    Manifest,
    ManifestError,
    Op,
    load_manifest,
    matches,
)

EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
CODE_LINE = re.compile(r"^Шифр документа: (SV-[A-Z]+-\d{3})$", re.M)


@pytest.fixture(scope="module")
def manifest() -> Manifest:
    return load_manifest()


def test_company_is_made_up(manifest: Manifest) -> None:
    assert manifest.domain == "severny-veter.test"
    assert manifest.company["headcount"] == 120
    assert len(manifest.employees) == 18
    assert {e.dept for e in manifest.employees.values()} == set(manifest.departments)
    for employee in manifest.employees.values():
        assert employee.email.endswith("@severny-veter.test")


def test_every_source_file_is_in_manifest_and_back(manifest: Manifest) -> None:
    on_disk = {
        str(path.relative_to(CORPUS)) for path in CORPUS.rglob("*.md") if path.is_file()
    }
    assert on_disk == {d.file for d in manifest.documents.values()}
    assert len(manifest.documents) >= 60


def test_documents_carry_their_code_and_title(manifest: Manifest) -> None:
    for document in manifest.documents.values():
        source = read_source(document.file)
        assert source.splitlines()[0] == f"# {document.title}", document.id
        assert CODE_LINE.findall(source) == [document.id], document.id
        assert document.zone in manifest.zones


def test_all_emails_in_corpus_are_fictional(manifest: Manifest) -> None:
    for document in manifest.documents.values():
        for address in EMAIL.findall(read_source(document.file)):
            assert address.endswith("@severny-veter.test"), (document.id, address)


def test_closed_documents_are_marked_closed(manifest: Manifest) -> None:
    """Зарплаты, приказы о премиях и споры — в закрытых зонах, и зона не
    открыта всем."""
    closed = {zone.id for zone in manifest.zones.values() if zone.closed}
    assert closed == {"payroll", "awards", "disputes"}
    for document in manifest.documents.values():
        text = read_source(document.file)
        if "Гриф: конфиденциально" in text:
            assert document.zone in closed, document.id
    for zone_id in closed:
        readers = manifest.readers_of(zone_id)
        assert 0 < len(readers) < len(manifest.employees) // 2, zone_id


def test_conflicts_are_really_in_the_sources(manifest: Manifest) -> None:
    assert len(manifest.conflicts) >= 6
    for conflict in manifest.conflicts:
        facts = conflict["facts"]
        assert isinstance(facts, list) and len(facts) >= 2
        for fact in facts:
            source = read_source(manifest.documents[fact["doc"]].file)
            assert fact["text"] in source, (conflict["topic"], fact["doc"])


def test_cloud_accounts_split_every_closed_zone(manifest: Manifest) -> None:
    """В облаке шесть учёток: у каждой закрытой зоны среди них есть и
    читатели, и не читатели — иначе проверка прав ничего не докажет."""
    cloud = set(manifest.cloud_accounts)
    for zone in manifest.zones.values():
        if zone.closed:
            readers = manifest.readers_of(zone.id) & cloud
            assert readers and cloud - readers, zone.id


def test_every_system_holds_closed_documents(manifest: Manifest) -> None:
    for system in manifest.systems.values():
        zones = {manifest.documents[d].zone for d in system.docs}
        assert zones & {"payroll", "awards", "disputes"}, system.id
        assert len(system.docs) == len(set(system.docs))


def test_every_step_touches_every_system(manifest: Manifest) -> None:
    for system in manifest.systems.values():
        for step in manifest.steps:
            expected = Expected(manifest, system)
            applied = [expected.apply(op) for op in step.ops]
            assert any(applied), (system.id, step.id)


def test_selectors(manifest: Manifest) -> None:
    people = manifest.employees
    assert matches("all", people["lednev"])
    assert matches("dept:legal", people["nikitina"])
    assert not matches("dept:legal", people["volkov"])
    assert matches("group:payroll", people["petrenko"])
    assert not matches("group:payroll", people["kuznetsova"])
    assert matches("login:vetrov", people["vetrov"])
    with pytest.raises(ManifestError):
        matches("team:x", people["vetrov"])


def test_expected_visibility_follows_zones(manifest: Manifest) -> None:
    expected = Expected(manifest, manifest.systems["nextcloud"])
    # Ведомость — расчётной группе, и только ей.
    assert expected.readers("SV-PAY-001") == {"vetrov", "belova", "gromova", "petrenko"}
    # Приказ о премии читают ещё и руководители отделов.
    assert "volkov" in expected.readers("SV-PAY-003")
    assert "petrenko" in expected.readers("SV-PAY-003")
    assert "kuznetsova" not in expected.readers("SV-PAY-003")
    # Юридический спор — юристы и директор, бухгалтерия нет.
    assert expected.readers("SV-LEG-004") == {"savelyev", "nikitina", "vetrov"}
    assert "SV-LEG-004" not in expected.visible_to("gromova")
    # Общий документ — всем.
    assert expected.readers("SV-GEN-001") == set(manifest.employees)


def test_steps_move_the_expectations(manifest: Manifest) -> None:
    expected = Expected(manifest, manifest.systems["bookstack"])
    assert "SV-PAY-001" in expected.visible_to("petrenko")

    expected.apply(Op("revoke", login="petrenko", zone="payroll"))
    assert "SV-PAY-001" not in expected.visible_to("petrenko")
    # Отзыв по зоне: приказы о премиях (другая зона) остались.
    assert "SV-PAY-003" in expected.visible_to("petrenko")

    expected.apply(Op("revoke", login="lebedeva"))
    assert expected.visible_to("lebedeva") == frozenset()

    expected.apply(Op("move", doc="SV-SAL-002", zone="management"))
    assert "SV-SAL-002" not in expected.visible_to("volkov")
    assert "SV-SAL-002" in expected.visible_to("snegova")

    expected.apply(Op("edit", doc="SV-SAL-001", text="ПРАВКА-1: цена"))
    assert expected.docs["SV-SAL-001"].edits == ["ПРАВКА-1: цена"]

    assert expected.apply(Op("delete", doc="SV-LEG-004"))
    assert "SV-LEG-004" not in expected.visible_to("savelyev")
    # Удалённый документ не удаляют и не правят второй раз.
    assert not expected.apply(Op("delete", doc="SV-LEG-004"))
    assert not expected.apply(Op("edit", doc="SV-LEG-004", text="x"))


def test_manifest_rejects_unknown_references(tmp_path: Path) -> None:
    source = (Path(__file__).parent / "manifest.toml").read_text(encoding="utf-8")
    broken = source.replace(
        '{ op = "edit", doc = "SV-SAL-001"', '{ op = "edit", doc = "SV-XXX-999"', 1
    )
    path = tmp_path / "manifest.toml"
    path.write_text(broken, encoding="utf-8")
    with pytest.raises(ManifestError):
        load_manifest(path)
