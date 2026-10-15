"""Сценарий одной системы: подключение, синхронизации и сверка с манифестом.

Порядок: компания и сотрудники в базе → подключение (и гранты сотрудников
для per_user) → синхронизация через `ConnectorSyncService` → сверка → то же
после повторной синхронизации без изменений → шаги изменений из манифеста,
после каждого — синхронизация и сверка.

Сверка — три вопроса:
1. все ли документы манифеста появились (и удалённые — исчезли);
2. каждый ли сотрудник видит ровно положенное: видимость считается так же,
   как её считает поиск ассистента (`chunk_repository.visible_to`), а не по
   сырым записям доступа; видимые документы, которых нет в его списке, —
   утечка, не видимые из списка — потеря доступа;
3. дошли ли правки (метка из шага есть в тексте материала).

Документ узнаётся по шифру в тексте («Шифр документа: SV-…»), а не по
названию или пути: названия в разных системах оформляются по-разному,
шифр — часть содержимого. Материалы без шифра (файлы-образцы, которые
система кладёт новым пользователям) в сверку не входят.
"""

import re
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.registry import AdapterRegistry, default_registry
from corp_ed.core import outbound
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.secrets import SecretBox
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import Connector, ConnectorUserGrant, Material, Tenant, User
from corp_ed.domain.types import ConnectorMode, SyncRunStatus, SyncTrigger
from corp_ed.repositories.chunk_repository import ChunkRepository
from corp_ed.services.connector_sync_service import (
    ConnectorSyncService,
    Extractor,
    SyncOutcome,
)
from tests.acceptance.database import create_employees, create_tenant
from tests.acceptance.model import Expected, Manifest, Op, Step, System
from tests.acceptance.systems.base import Driver, NotSupportedError, Placement

CODE = re.compile(r"Шифр документа[:\s|*]*(SV-[A-Z]+-\d{3})")
MARKER = re.compile(r"^(\S+?):")

OK, FAIL, SKIP = "ok", "fail", "skip"
INITIAL = "sync"
RESYNC = "resync"


@dataclass
class StepResult:
    system: str
    step: str
    status: str
    details: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "system": self.system,
            "step": self.step,
            "status": self.status,
            "details": self.details,
            "stats": self.stats,
        }


@dataclass
class Observation:
    """Что ядро kronto видит в подключении сейчас."""

    contents: dict[str, list[str]]
    """шифр → тексты всех материалов с этим шифром"""
    seen: dict[str, set[str]]
    """логин → шифры документов, видимых ему по правилу ядра"""
    without_code: int = 0


def marker_of(text: str) -> str:
    found = MARKER.match(text)
    return found.group(1) if found else text[:20]


# --- сверка ------------------------------------------------------------------------


def compare(expected: Expected, obs: Observation, logins: list[str]) -> list[str]:
    """Расхождения между манифестом и тем, что увидело ядро."""
    problems: list[str] = []
    wanted = {state.document.id for state in expected.present_docs()}
    for doc_id in sorted(wanted - obs.contents.keys()):
        title = expected.docs[doc_id].document.title
        problems.append(f"нет документа {doc_id} «{title}»")
    for doc_id in sorted(obs.contents.keys() - wanted):
        gone = doc_id in expected.docs
        problems.append(
            f"документ {doc_id} должен был исчезнуть, но остался"
            if gone
            else f"лишний документ {doc_id}"
        )
    for state in expected.present_docs():
        texts = obs.contents.get(state.document.id)
        for edit in state.edits:
            token = marker_of(edit)
            if texts and not all(token in text for text in texts):
                problems.append(f"правка {token} не дошла до {state.document.id}")
    for login in logins:
        should = expected.visible_to(login)
        seen = obs.seen.get(login, set())
        for doc_id in sorted(seen - should):
            problems.append(f"УТЕЧКА: {login} видит {doc_id}, а не должен")
        for doc_id in sorted(should - seen):
            problems.append(f"{login} не видит {doc_id}, а должен")
    return problems


async def observe(
    session_maker: async_sessionmaker[AsyncSession],
    tenant: Tenant,
    connector_id: Any,
    users: dict[str, User],
) -> Observation:
    contents: dict[str, list[str]] = {}
    ids_by_code: dict[str, list[Any]] = {}
    without_code = 0
    with tenant_scope(tenant.id):
        async with session_maker() as session:
            rows = await session.execute(
                select(Material.id, Material.content).where(
                    Material.connector_id == connector_id
                )
            )
            for material_id, content in rows:
                found = CODE.search(content or "")
                if found is None:
                    without_code += 1
                    continue
                contents.setdefault(found.group(1), []).append(content or "")
                ids_by_code.setdefault(found.group(1), []).append(material_id)
            by_id = {i: code for code, ids in ids_by_code.items() for i in ids}
            chunks = ChunkRepository(session)
            seen: dict[str, set[str]] = {}
            for login, user in users.items():
                visible = await chunks.visible_material_ids(by_id, viewer=user.id)
                seen[login] = {by_id[i] for i in visible}
    return Observation(contents, seen, without_code)


# --- подключение ---------------------------------------------------------------------


@dataclass
class Setup:
    tenant: Tenant
    users: dict[str, User]
    connector: Connector
    service: ConnectorSyncService
    logins: list[str]


@contextmanager
def _stand_trust(driver: Driver, ca: str) -> Iterator[None]:
    url = driver.stand_url()
    if url is None:
        yield
        return
    from tests.live.dav_stand import local_stand

    with local_stand(url):
        yield


@asynccontextmanager
async def http_for(
    driver: Driver, settings: ConnectorSettings, ca: str
) -> AsyncIterator[outbound.OutboundClient]:
    """Клиент наружу: к Docker-стенду — с доверием к его сертификату, к
    облаку — как в продукте (outbound_http_client)."""
    url = driver.stand_url()
    if url is None:
        raw = outbound.outbound_http_client(via_proxy=settings.outbound_via_proxy)
    else:
        from tests.live.dav_stand import stand_http

        raw = stand_http(ca)
    try:
        yield outbound.OutboundClient(
            raw,
            via_proxy=settings.outbound_via_proxy,
            download_deadline=settings.download_timeout_seconds,
        )
    finally:
        await raw.aclose()


def effective_logins(system: System, driver: Driver) -> list[str]:
    """Чьи учётные записи в системе есть и чьи данные у нас есть."""
    if driver.mode is ConnectorMode.PER_USER and driver.creds is not None:
        return [login for login in system.accounts if login in driver.creds.users]
    return list(system.accounts)


async def build_setup(
    session_maker: async_sessionmaker[AsyncSession],
    manifest: Manifest,
    system: System,
    driver: Driver,
    registry: AdapterRegistry,
    http: outbound.OutboundClient,
    settings: ConnectorSettings,
    key: str,
    extractor: Extractor | None = None,
) -> Setup:
    assert driver.creds is not None
    secrets = SecretBox([key])
    logins = effective_logins(system, driver)
    async with session_maker() as session:
        tenant = await create_tenant(session, system.id)
        people = {
            login: (manifest.employees[login].email, manifest.employees[login].name)
            for login in logins
        }
        users = await create_employees(session, tenant, people)
        modules = list(driver.modules) or [registry.spec(system.kind).modules[0].name]
        with tenant_scope(tenant.id):
            connector = Connector(
                kind=system.kind,
                name=f"{system.title} (приёмка)",
                mode=driver.mode.value,
                modules=modules,
                config=driver.config(),
                credentials=(
                    secrets.encrypt(
                        (driver.creds.organization or {})
                        if driver.mode is ConnectorMode.ORGANIZATION
                        else (driver.creds.app or {})
                    )
                    if driver.mode is ConnectorMode.ORGANIZATION or driver.creds.app
                    else None
                ),
                credentials_set_at=datetime.now(UTC),
            )
            session.add(connector)
            await session.commit()
            if driver.mode is ConnectorMode.PER_USER:
                for login in logins:
                    session.add(
                        ConnectorUserGrant(
                            connector_id=connector.id,
                            user_id=users[login].id,
                            credentials=secrets.encrypt(driver.creds.users[login]),
                        )
                    )
                await session.commit()
    options: dict[str, Any] = {} if extractor is None else {"extractor": extractor}
    service = ConnectorSyncService(
        session_maker, http, registry, secrets, settings, **options
    )
    return Setup(tenant, users, connector, service, logins)


def make_settings(key: str, **overrides: Any) -> ConnectorSettings:
    """Бюджет запуска — максимальный: иначе большая компания не успеет за
    один запуск, ядро не доведёт обход до конца и удаления не произойдут."""
    return ConnectorSettings(  # type: ignore[call-arg]
        secrets_keys=key,
        max_documents_per_run=5000,
        max_run_minutes=60,
        **overrides,
    )


def new_key() -> str:
    return Fernet.generate_key().decode()


# --- шаги -----------------------------------------------------------------------------


async def sync_once(setup: Setup) -> SyncOutcome:
    outcome = await setup.service.run(
        setup.connector.tenant_id, setup.connector.id, trigger=SyncTrigger.MANUAL
    )
    assert outcome is not None, "подключение не активно (остановлено ядром)"
    return outcome


def _stats(outcome: SyncOutcome) -> dict[str, Any]:
    return {
        "status": outcome.status.value,
        "error_code": outcome.error_code,
        **outcome.stats.as_dict(),
    }


def _outcome_problems(outcome: SyncOutcome) -> list[str]:
    problems = []
    if outcome.status is not SyncRunStatus.SUCCEEDED:
        problems.append(
            f"синхронизация: {outcome.status.value}"
            + (f" ({outcome.error_code})" if outcome.error_code else "")
        )
    if outcome.stats.failed:
        problems.append(f"документов с ошибкой: {outcome.stats.failed}")
    return problems


def _placement(
    expected: Expected, system: System, doc_id: str, extra: tuple[str, ...] = ()
) -> Placement:
    state = expected.docs[doc_id]
    fmt = "md" if system.storage == "pages" else state.document.format
    return Placement(state.document, state.zone, fmt, (*state.edits, *extra))


def apply_op(driver: Driver, expected: Expected, op: Op) -> str | None:
    """Выполнить операцию в системе и сдвинуть ожидания.

    Возвращает None — сделано, иначе причину пропуска. NotSupportedError
    не меняет ожидания: система осталась как была."""
    system = driver.system
    if op.op == "revoke":
        assert op.login
        if op.login not in system.accounts:
            return f"у {op.login} нет записи в системе"
        zones = (
            [op.zone]
            if op.zone
            else [z for z, m in expected.members.items() if op.login in m]
        )
        zones = [z for z in zones if op.login in expected.members[z]]
        if not zones:
            return f"у {op.login} и так нет доступа"
        driver.revoke(op.login, zones)
    else:
        assert op.doc
        state = expected.docs.get(op.doc)
        if state is None or not state.present:
            return f"{op.doc} нет в системе"
        if op.op == "edit":
            assert op.text
            driver.edit(_placement(expected, system, op.doc, (op.text,)))
        elif op.op == "delete":
            driver.delete(_placement(expected, system, op.doc))
        elif op.op == "move":
            assert op.zone
            driver.move(_placement(expected, system, op.doc), op.zone)
    expected.apply(op)
    return None


async def run_step(
    step: Step, setup: Setup, driver: Driver, expected: Expected, system: System
) -> StepResult:
    result = StepResult(system.id, step.id, OK)
    skipped: list[str] = []
    applied = {"edit": 0, "delete": 0, "revoke": 0, "move": 0}
    for op in step.ops:
        try:
            reason = apply_op(driver, expected, op)
        except NotSupportedError as exc:
            skipped.append(f"{op.op} {op.doc or op.login}: {exc}")
            continue
        except Exception as exc:  # операция упала в самой системе
            result.status = FAIL
            result.details.append(
                f"{op.op} {op.doc or op.login}: {type(exc).__name__}: {exc}"
            )
            return result
        if reason:
            skipped.append(f"{op.op} {op.doc or op.login}: {reason}")
        else:
            applied[op.op] += 1
    if not any(applied.values()):
        result.status = SKIP
        result.details = skipped or ["нечего применять"]
        return result
    outcome = await sync_once(setup)
    result.stats = _stats(outcome) | {"applied": applied}
    problems = _outcome_problems(outcome)
    if applied["edit"] and outcome.stats.updated < applied["edit"]:
        problems.append(f"обновлено {outcome.stats.updated} < правок {applied['edit']}")
    if applied["delete"] and outcome.stats.removed < applied["delete"]:
        problems.append(
            f"удалено {outcome.stats.removed} < удалений {applied['delete']}"
        )
    obs = await observe(
        setup.service.session_maker, setup.tenant, setup.connector.id, setup.users
    )
    problems += compare(expected, obs, setup.logins)
    result.details = problems + [f"пропущено: {s}" for s in skipped]
    result.status = FAIL if problems else OK
    return result


async def run_initial(
    setup: Setup, expected: Expected, system: System
) -> list[StepResult]:
    """Первая синхронизация и повторная без изменений."""
    first = StepResult(system.id, INITIAL, OK)
    outcome = await sync_once(setup)
    first.stats = _stats(outcome)
    problems = _outcome_problems(outcome)
    present = len(expected.present_docs())
    if outcome.stats.added < present:
        problems.append(
            f"добавлено {outcome.stats.added} < документов манифеста {present}"
        )
    obs = await observe(
        setup.service.session_maker, setup.tenant, setup.connector.id, setup.users
    )
    problems += compare(expected, obs, setup.logins)
    if obs.without_code:
        first.stats["without_code"] = obs.without_code
    first.details = problems
    first.status = FAIL if problems else OK

    second = StepResult(system.id, RESYNC, OK)
    outcome = await sync_once(setup)
    second.stats = _stats(outcome)
    problems = _outcome_problems(outcome)
    stats = outcome.stats
    if stats.added or stats.updated or stats.removed:
        problems.append(
            f"повторный запуск без изменений: добавлено {stats.added}, "
            f"обновлено {stats.updated}, удалено {stats.removed}"
        )
    obs = await observe(
        setup.service.session_maker, setup.tenant, setup.connector.id, setup.users
    )
    problems += compare(expected, obs, setup.logins)
    second.details = problems
    second.status = FAIL if problems else OK
    return [first, second]


async def run_system(
    manifest: Manifest,
    system: System,
    driver: Driver,
    session_maker: async_sessionmaker[AsyncSession],
    registry: AdapterRegistry,
    *,
    ca: str,
    steps: list[str] | None = None,
    extractor: Extractor | None = None,
) -> list[StepResult]:
    """Все шаги одной системы; драйвер уже имеет учётные данные."""
    all_ids = [INITIAL, RESYNC, *(s.id for s in manifest.steps)]
    wanted = steps or all_ids
    reason = driver.unavailable()
    if reason:
        return [StepResult(system.id, sid, SKIP, [reason]) for sid in wanted]
    key = new_key()
    settings = make_settings(key)
    logins = effective_logins(system, driver)
    expected = Expected(manifest, replace(system, accounts=tuple(logins)))
    results: list[StepResult] = []
    with _stand_trust(driver, ca):
        async with http_for(driver, settings, ca) as http:
            setup = await build_setup(
                session_maker,
                manifest,
                system,
                driver,
                registry,
                http,
                settings,
                key,
                extractor,
            )
            first = await run_initial(setup, expected, system)
            results += [r for r in first if r.step in wanted]
            # Первая синхронизация не прошла — шаги изменений бессмысленны.
            broken = first[0].status != OK
            for step in manifest.steps:
                if step.id not in wanted:
                    continue
                if broken:
                    results.append(
                        StepResult(
                            system.id,
                            step.id,
                            SKIP,
                            ["начальная синхронизация не прошла"],
                        )
                    )
                    continue
                results.append(await run_step(step, setup, driver, expected, system))
    return results


def default_registry_for(key: str) -> AdapterRegistry:
    return default_registry(make_settings(key))
