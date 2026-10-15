"""Манифест приёмки: загрузка, разбор селекторов и ожидаемое состояние.

Ожидаемое состояние — то, что манифест обещает про систему сейчас: какие
документы лежат в какой зоне и кто в зоне может читать. Шаги изменений
(`apply`) сдвигают его так же, как сдвигают сами системы; сценарий после
каждого шага сверяет с ним то, что увидело ядро kronto.
"""

import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "manifest.toml"

OPS = ("edit", "delete", "revoke", "move")


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class Employee:
    login: str
    email: str
    name: str
    dept: str
    title: str
    groups: tuple[str, ...]


@dataclass(frozen=True)
class Zone:
    id: str
    title: str
    readers: tuple[str, ...]
    closed: bool = False


@dataclass(frozen=True)
class Document:
    id: str
    file: str
    title: str
    zone: str
    format: str
    folder: str


@dataclass(frozen=True)
class System:
    id: str
    kind: str
    title: str
    stand: str  # docker | cloud
    storage: str  # files | pages
    accounts: tuple[str, ...]  # логины с учётной записью в системе
    docs: tuple[str, ...]  # id документов, лежащих в системе
    modules: tuple[str, ...] = ()  # модули подключения; пусто — первый у вида
    synced_zones: tuple[str, ...] | None = None
    """Зоны, которые подключение вообще забирает; None — все. Нужно виду,
    который не умеет разные права у разных зон одного подключения
    (MediaWiki: права на вики, а не на пространство имён): остальные зоны в
    системе лежат, но в kronto их быть не должно."""

    @property
    def cloud(self) -> bool:
        return self.stand == "cloud"


@dataclass(frozen=True)
class Op:
    op: str
    doc: str | None = None
    login: str | None = None
    zone: str | None = None
    text: str | None = None


@dataclass(frozen=True)
class Step:
    id: str
    title: str
    ops: tuple[Op, ...]


@dataclass(frozen=True)
class Manifest:
    company: dict[str, object]
    departments: dict[str, str]
    employees: dict[str, Employee]
    zones: dict[str, Zone]
    documents: dict[str, Document]
    systems: dict[str, System]
    steps: tuple[Step, ...]
    conflicts: tuple[dict[str, object], ...]
    cloud_accounts: tuple[str, ...]

    @property
    def domain(self) -> str:
        return str(self.company["domain"])

    def readers_of(self, zone: str) -> frozenset[str]:
        """Логины всех сотрудников компании, кому зона открыта."""
        return frozenset(
            e.login
            for e in self.employees.values()
            if any(matches(selector, e) for selector in self.zones[zone].readers)
        )


def matches(selector: str, employee: Employee) -> bool:
    """Селектор readers: all | dept:<отдел> | group:<группа> | login:<логин>."""
    if selector == "all":
        return True
    kind, _, value = selector.partition(":")
    if kind == "dept":
        return employee.dept == value
    if kind == "group":
        return value in employee.groups
    if kind == "login":
        return employee.login == value
    raise ManifestError(f"неизвестный селектор: {selector}")


def load_manifest(path: Path = MANIFEST) -> Manifest:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    employees = {
        item["login"]: Employee(
            login=item["login"],
            email=item["email"],
            name=item["name"],
            dept=item["dept"],
            title=item["title"],
            groups=tuple(item["groups"]),
        )
        for item in raw["employees"]
    }
    zones = {
        zone_id: Zone(
            zone_id,
            item["title"],
            tuple(item["readers"]),
            bool(item.get("closed", False)),
        )
        for zone_id, item in raw["zones"].items()
    }
    documents = {
        item["id"]: Document(
            id=item["id"],
            file=item["file"],
            title=item["title"],
            zone=item["zone"],
            format=item["format"],
            folder=item["folder"],
        )
        for item in raw["documents"]
    }
    bundles: dict[str, list[str]] = raw["bundles"]
    cloud_accounts = tuple(raw["cloud"]["accounts"])
    systems = {}
    for system_id, item in raw["systems"].items():
        docs: list[str] = []
        for entry in item["docs"]:
            docs.extend(bundles[entry[1:]] if entry.startswith("@") else [entry])
        accounts = item["accounts"]
        systems[system_id] = System(
            id=system_id,
            kind=item["kind"],
            title=item["title"],
            stand=item["stand"],
            storage=item["storage"],
            modules=tuple(item.get("modules", ())),
            synced_zones=(
                tuple(item["synced_zones"]) if "synced_zones" in item else None
            ),
            accounts=(
                tuple(employees)
                if accounts == "all"
                else cloud_accounts
                if accounts == "cloud"
                else tuple(accounts)
            ),
            # порядок сохраняется, повторы (документ в двух наборах) уходят
            docs=tuple(dict.fromkeys(docs)),
        )
    steps = tuple(
        Step(
            item["id"],
            item["title"],
            tuple(Op(**op) for op in item["ops"]),
        )
        for item in raw["steps"]
    )
    manifest = Manifest(
        company=raw["company"],
        departments=raw["departments"],
        employees=employees,
        zones=zones,
        documents=documents,
        systems=systems,
        steps=steps,
        conflicts=tuple(raw.get("conflicts", ())),
        cloud_accounts=cloud_accounts,
    )
    validate(manifest)
    return manifest


def validate(manifest: Manifest) -> None:
    """Согласованность манифеста: ссылки существуют, почты — вымышленные."""
    domain = manifest.domain
    for employee in manifest.employees.values():
        if employee.email != f"{employee.login}@{domain}":
            raise ManifestError(f"почта {employee.email} не <логин>@{domain}")
        if employee.dept not in manifest.departments:
            raise ManifestError(f"{employee.login}: нет отдела {employee.dept}")
    for zone in manifest.zones.values():
        for selector in zone.readers:
            # Разбор падает на неизвестном селекторе.
            if not any(matches(selector, e) for e in manifest.employees.values()):
                raise ManifestError(f"зона {zone.id}: селектор {selector} пуст")
    for document in manifest.documents.values():
        if document.zone not in manifest.zones:
            raise ManifestError(f"{document.id}: нет зоны {document.zone}")
    for system in manifest.systems.values():
        for doc_id in system.docs:
            if doc_id not in manifest.documents:
                raise ManifestError(f"{system.id}: нет документа {doc_id}")
        for login in system.accounts:
            if login not in manifest.employees:
                raise ManifestError(f"{system.id}: нет сотрудника {login}")
        for zone in system.synced_zones or ():
            if zone not in manifest.zones:
                raise ManifestError(f"{system.id}: нет зоны {zone}")
    for conflict in manifest.conflicts:
        for fact in conflict["facts"]:  # type: ignore[attr-defined]
            if fact["doc"] not in manifest.documents:
                raise ManifestError(f"противоречие: нет документа {fact['doc']}")
    for step in manifest.steps:
        for op in step.ops:
            if op.op not in OPS:
                raise ManifestError(f"шаг {step.id}: неизвестная операция {op.op}")
            if op.doc is not None and op.doc not in manifest.documents:
                raise ManifestError(f"шаг {step.id}: нет документа {op.doc}")
            if op.login is not None and op.login not in manifest.employees:
                raise ManifestError(f"шаг {step.id}: нет сотрудника {op.login}")
            if op.zone is not None and op.zone not in manifest.zones:
                raise ManifestError(f"шаг {step.id}: нет зоны {op.zone}")
            if op.op in ("edit", "delete", "move") and op.doc is None:
                raise ManifestError(f"шаг {step.id}: {op.op} без документа")
            if op.op == "edit" and not op.text:
                raise ManifestError(f"шаг {step.id}: edit без text")
            if op.op == "move" and op.zone is None:
                raise ManifestError(f"шаг {step.id}: move без zone")
            if op.op == "revoke" and op.login is None:
                raise ManifestError(f"шаг {step.id}: revoke без login")


# --- ожидаемое состояние -------------------------------------------------------


@dataclass
class DocState:
    document: Document
    zone: str
    present: bool = True
    edits: list[str] = field(default_factory=list)


class Expected:
    """Что должно быть в системе сейчас. Начальное состояние берётся из
    манифеста; `apply` сдвигает его операцией шага."""

    def __init__(self, manifest: Manifest, system: System) -> None:
        self.manifest = manifest
        self.system = system
        accounts = set(system.accounts)
        self.members: dict[str, set[str]] = {
            zone: set(manifest.readers_of(zone)) & accounts for zone in manifest.zones
        }
        self.docs: dict[str, DocState] = {
            doc_id: DocState(
                manifest.documents[doc_id], manifest.documents[doc_id].zone
            )
            for doc_id in system.docs
        }

    def apply(self, op: Op) -> bool:
        """Сдвинуть состояние; False — операция к этой системе не относится
        (документа в ней нет), её пропускают и в самой системе."""
        if op.op == "revoke":
            assert op.login is not None
            zones = [op.zone] if op.zone else list(self.members)
            if op.login not in self.system.accounts:
                return False
            for zone in zones:
                self.members[zone].discard(op.login)
            return True
        assert op.doc is not None
        state = self.docs.get(op.doc)
        if state is None or not state.present:
            return False
        if op.op == "edit":
            assert op.text
            state.edits.append(op.text)
        elif op.op == "delete":
            state.present = False
        elif op.op == "move":
            assert op.zone
            state.zone = op.zone
        return True

    def present_docs(self) -> list[DocState]:
        """Документы, которые должны быть в kronto: лежат в системе и в
        зоне, которую подключение забирает."""
        return [state for state in self.docs.values() if self._in_kronto(state)]

    def _in_kronto(self, state: DocState) -> bool:
        synced = self.system.synced_zones
        return state.present and (synced is None or state.zone in synced)

    def readers(self, doc_id: str) -> frozenset[str]:
        state = self.docs[doc_id]
        return (
            frozenset(self.members[state.zone])
            if self._in_kronto(state)
            else frozenset()
        )

    def visible_to(self, login: str) -> frozenset[str]:
        """Документы, которые login ДОЛЖЕН видеть."""
        return frozenset(
            s.document.id for s in self.present_docs() if login in self.members[s.zone]
        )

    def hidden_from(self, login: str) -> frozenset[str]:
        """Документы, которые login НЕ должен видеть (лежат, но закрыты)."""
        return frozenset(s.document.id for s in self.present_docs()) - self.visible_to(
            login
        )


def ops_for(step: Step, ids: Iterable[str]) -> list[Op]:
    """Операции шага, которые касаются документов из ids (и все revoke)."""
    wanted = set(ids)
    return [op for op in step.ops if op.doc is None or op.doc in wanted]
