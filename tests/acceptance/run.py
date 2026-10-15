"""Приёмка коннекторов: `python -m tests.acceptance.run --system <вид> ...`

Сценарий идёт в этом же процессе, без поднятого API: заводит в базе
(ACCEPTANCE_DATABASE_URL или TEST_DATABASE_URL) компанию и сотрудников из
манифеста, подключение выбранного вида с учётными данными из файла или
переменных окружения, гоняет `ConnectorSyncService` и сверяет с манифестом
то, что увидело ядро (документы, права каждого сотрудника, шаги изменений).
Итог — таблица «система × шаг: ok/fail/skip» в stdout и JSON-файл. Качество
ответов ассистента не проверяется.

    # Docker-стенд: поднять, засеять корпусом и правами, проверить
    python -m tests.acceptance.stand up bookstack
    python -m tests.acceptance.run --system bookstack --seed

    # облачный кабинет: учётные данные из файла (OWNER-CHECKLIST.md)
    python -m tests.acceptance.run --system kaiten \\
        --credentials ~/.kronto-acc/cloud.json

    python -m tests.acceptance.run --list     # системы и готовность
    python -m tests.acceptance.run --plan --system nextcloud   # что куда

Код выхода: 1 — есть fail; 0 — всё ok или skip.
"""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from tests.acceptance.database import open_database
from tests.acceptance.model import Manifest, System, load_manifest
from tests.acceptance.scenario import (
    FAIL,
    INITIAL,
    OK,
    RESYNC,
    SKIP,
    StepResult,
    default_registry_for,
    new_key,
    run_system,
)
from tests.acceptance.stand import stand_url
from tests.acceptance.systems import make_driver, resolve_credentials
from tests.acceptance.systems.base import (
    Creds,
    credentials_path,
    write_credentials,
)

DEFAULT_CREDENTIALS = "~/.kronto-acc/credentials.json"


def _select(manifest: Manifest, names: Sequence[str]) -> list[System]:
    if not names or names == ["all"]:
        return list(manifest.systems.values())
    chosen: list[System] = []
    for name in names:
        if name == "docker":
            chosen += [s for s in manifest.systems.values() if not s.cloud]
        elif name == "cloud":
            chosen += [s for s in manifest.systems.values() if s.cloud]
        elif name in manifest.systems:
            chosen.append(manifest.systems[name])
        else:
            by_kind = [s for s in manifest.systems.values() if s.kind == name]
            if not by_kind:
                raise SystemExit(
                    f"нет системы «{name}»; есть: {', '.join(manifest.systems)}"
                )
            chosen += by_kind
    return list(dict.fromkeys(chosen))


def format_table(
    manifest: Manifest, results: list[StepResult], systems: list[System]
) -> str:
    steps = [INITIAL, RESYNC, *(s.id for s in manifest.steps)]
    by_key = {(r.system, r.step): r for r in results}
    width = max(len(s.id) for s in systems) + 2
    head = "система".ljust(width) + "".join(step.ljust(9) for step in steps)
    lines = [head, "-" * len(head)]
    for system in systems:
        cells = []
        for step in steps:
            result = by_key.get((system.id, step))
            cells.append((result.status if result else "-").ljust(9))
        lines.append(system.id.ljust(width) + "".join(cells))
    return "\n".join(lines)


def format_details(results: list[StepResult]) -> str:
    out = []
    for result in results:
        if result.status == OK or not result.details:
            continue
        out.append(f"[{result.status}] {result.system} / {result.step}")
        out += [f"    {line}" for line in result.details[:40]]
        if len(result.details) > 40:
            out.append(f"    … и ещё {len(result.details) - 40}")
    return "\n".join(out)


def print_plan(manifest: Manifest, system: System) -> None:
    from tests.acceptance.model import Expected

    expected = Expected(manifest, system)
    print(f"{system.id} ({system.title}), хранение: {system.storage}")
    print(f"учётные записи: {', '.join(system.accounts)}")
    for state in expected.docs.values():
        zone = manifest.zones[state.zone]
        fmt = "md" if system.storage == "pages" else state.document.format
        readers = ", ".join(sorted(expected.members[state.zone])) or "—"
        print(
            f"  {state.document.id}  {zone.title}/{state.document.folder}/"
            f"{state.document.title}.{fmt}\n      видят: {readers}"
        )


def print_list(manifest: Manifest, path: Path | None) -> None:
    for system in manifest.systems.values():
        driver = make_driver(
            system, manifest, resolve_credentials(system, manifest, path)
        )
        reason = driver.unavailable()
        state = "готова к проверке" if reason is None else f"нет данных: {reason}"
        seed = ", засев умеет" if driver.seedable else ""
        print(f"{system.id:18} {system.stand:7} {system.kind:18} {state}{seed}")


async def _amain(args: argparse.Namespace) -> int:
    manifest = load_manifest()
    path = credentials_path(args.credentials or DEFAULT_CREDENTIALS)
    if args.list:
        print_list(manifest, path)
        return 0
    systems = _select(manifest, args.system)
    if args.plan:
        for system in systems:
            print_plan(manifest, system)
        return 0

    results: list[StepResult] = []
    key = new_key()
    registry = default_registry_for(key)
    async with open_database(args.database_url, reset=args.reset_db) as session_maker:
        for system in systems:
            creds = resolve_credentials(system, manifest, path)
            if args.seed and not system.cloud:
                creds = Creds(extra={"url": stand_url(system.id)})
            driver = make_driver(system, manifest, creds)
            if args.seed:
                creds = await _seed(system, manifest, driver, creds, path)
                if creds is None:
                    results += [
                        StepResult(
                            system.id, s, SKIP, ["засев не удался или не поддержан"]
                        )
                        for s in [INITIAL, RESYNC, *(x.id for x in manifest.steps)]
                    ]
                    continue
                driver = make_driver(system, manifest, creds)
            print(f"== {system.id} ({system.title})", flush=True)
            results += await run_system(
                manifest,
                system,
                driver,
                session_maker,
                registry,
                ca=os.environ.get("DAV_STAND_CA", ""),
                steps=args.steps.split(",") if args.steps else None,
            )
    table = format_table(manifest, results, systems)
    print()
    print(table)
    details = format_details(results)
    if details:
        print()
        print(details)
    if args.json:
        payload = {
            "finished_at": datetime.now(UTC).isoformat(),
            "company": manifest.company["name"],
            "results": [r.as_json() for r in results],
            "summary": {
                status: sum(1 for r in results if r.status == status)
                for status in (OK, FAIL, SKIP)
            },
        }
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return 1 if any(r.status == FAIL for r in results) else 0


async def _seed(
    system: System,
    manifest: Manifest,
    driver: object,
    creds: Creds | None,
    path: Path | None,
) -> Creds | None:
    from tests.acceptance.model import Expected
    from tests.acceptance.systems.base import Driver, NotSupportedError

    assert isinstance(driver, Driver)
    if not driver.seedable:
        print(f"{system.id}: засев не написан для этой системы", file=sys.stderr)
        return None
    try:
        print(f"== засев {system.id}", flush=True)
        seeded = await asyncio.to_thread(driver.seed, Expected(manifest, system))
    except NotSupportedError as exc:
        print(f"{system.id}: {exc}", file=sys.stderr)
        return None
    if path is not None:
        write_credentials(path, system.id, seeded)
        print(f"учётные данные стенда записаны в {path}")
    return seeded


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.acceptance.run",
        description="Приёмка коннекторов kronto на вымышленной компании.",
    )
    parser.add_argument(
        "--system",
        nargs="+",
        default=[],
        help="система (bookstack, nextcloud, kaiten, …), вид, docker, cloud или all",
    )
    parser.add_argument("--credentials", help="файл учётных данных (JSON)")
    parser.add_argument("--seed", action="store_true", help="сначала засеять стенд")
    parser.add_argument("--steps", help="только эти шаги: sync,resync,edit,…")
    parser.add_argument("--json", help="записать итог в JSON-файл")
    parser.add_argument("--database-url", help="база; иначе ACCEPTANCE_DATABASE_URL")
    parser.add_argument("--reset-db", action="store_true", help="пересоздать схему")
    parser.add_argument("--list", action="store_true", help="системы и готовность")
    parser.add_argument("--plan", action="store_true", help="что куда и кто видит")
    args = parser.parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
