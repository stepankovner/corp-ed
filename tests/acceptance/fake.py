"""Поддельная система для самопроверки сценария приёмки.

Драйвер хранит документы в `FakeSource` (tests/fake_connector.py) и
отдаёт их ядру как документы с правами по почтам. Так весь сценарий —
базы, подключение, синхронизации, сверка, шаги — проходит без Docker, а
тест `test_scenario.py` проверяет и сам сценарий: он обязан заметить утечку
и непрошедший отзыв доступа.

`leak_zones` — зоны, документы которых драйвер ошибочно отдаёт «всем»;
`ignore_revoke` — драйвер делает вид, что доступ отозвал.
"""

from collections.abc import Iterable

from corp_ed.connectors.base import FetchedFile, RemoteDocument
from corp_ed.domain.types import (
    ConnectorMode,
    MaterialVisibility,
    RemoteDocumentKind,
)
from tests.acceptance.model import Expected, Manifest, System
from tests.acceptance.systems.base import Creds, Driver, Placement, placements
from tests.fake_connector import FAKE_KIND, FakeSource


class FakeDriver(Driver):
    mode = ConnectorMode.ORGANIZATION
    modules = ("docs",)
    seedable = True

    def __init__(
        self,
        system: System,
        manifest: Manifest,
        source: FakeSource,
        *,
        leak_zones: Iterable[str] = (),
        ignore_revoke: bool = False,
    ) -> None:
        super().__init__(
            system,
            manifest,
            Creds(
                config={"base_url": "https://portal.example.com/"},
                organization={"token": "t-acceptance"},
            ),
        )
        self.source = source
        self.leak_zones = set(leak_zones)
        self.ignore_revoke = ignore_revoke
        self._members: dict[str, set[str]] = {}
        self._docs: dict[str, Placement] = {}

    def seed(self, expected: Expected) -> Creds:
        self._members = {zone: set(m) for zone, m in expected.members.items()}
        self._docs = {p.document.id: p for p in placements(expected, self.system)}
        self._publish()
        assert self.creds is not None
        return self.creds

    def _publish(self) -> None:
        self.source.documents.clear()
        for doc_id, placement in self._docs.items():
            members = self._members[placement.zone]
            # «Виден всем» — только если зона открыта всем учёткам системы:
            # так отдают общие документы и BookStack, и группа Nextcloud.
            open_to_all = placement.zone in self.leak_zones or members >= set(
                self.system.accounts
            )
            version = f"{placement.zone}:{len(placement.edits)}"
            self.source.add(
                RemoteDocument(
                    external_id=doc_id,
                    title=placement.document.title,
                    url=f"https://portal.example.com/docs/{doc_id}",
                    version=version,
                    kind=RemoteDocumentKind.FILE,
                    module="docs",
                    filename=f"{doc_id}.md",
                    visibility=(
                        MaterialVisibility.TENANT
                        if open_to_all
                        else MaterialVisibility.RESTRICTED
                    ),
                    allowed_emails=frozenset(
                        self.manifest.employees[login].email for login in members
                    ),
                )
            )
            self.source.contents[doc_id] = FetchedFile(
                placement.content(), f"{doc_id}.md"
            )

    def edit(self, placement: Placement) -> None:
        self._docs[placement.document.id] = placement
        self._publish()

    def delete(self, placement: Placement) -> None:
        del self._docs[placement.document.id]
        self._publish()

    def revoke(self, login: str, zones: list[str]) -> None:
        if not self.ignore_revoke:
            for zone in zones:
                self._members[zone].discard(login)
        self._publish()

    def move(self, placement: Placement, to_zone: str) -> None:
        current = self._docs[placement.document.id]
        self._docs[placement.document.id] = Placement(
            current.document, to_zone, current.fmt, current.edits
        )
        self._publish()


def fake_system(manifest: Manifest, base_id: str = "bookstack") -> System:
    """Система из манифеста, но с видом поддельного источника."""
    from dataclasses import replace

    return replace(manifest.systems[base_id], id="fake", kind=FAKE_KIND, stand="docker")
