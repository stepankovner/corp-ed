"""Генератор нагрузки (docs/LOAD-TEST.md) — ТОЛЬКО локально.

Токены выпускает сам ключом SECRET_KEY по идентификаторам из seed.py:
второй фактор и вход в нагрузке не участвуют. Вопросы — из корпуса
проверки стенда (tests/fixtures/stand_quality/golden.json).

    corpus    загрузить документы корпуса администратором и дождаться индекса
    cycle     сотрудники как в жизни: открыли kronto (профиль, диалоги,
              уведомления, подсказки), вопрос, пауза, уточнение, пауза…;
              колокольчик опрашивается раз в минуту
    capacity  N одновременных вопросов без пауз — пропускная способность;
              сотрудники берутся по кругу из пула (лимит 30 вопросов в
              минуту на человека)
    burst     N сотрудников спрашивают в одну секунду («понедельник, 9:00»)
    search    N одновременных /faq/search без пауз — только поиск, без модели

Задержки — от первого байта запроса: «первое слово» — первое событие
delta потока, «ответ» — событие done. Процессор API и Postgres — psutil,
соединения — pg_stat_activity (OWNER_DATABASE_URL).
"""

import argparse
import asyncio
import contextlib
import itertools
import json
import os
import random
import statistics
import time
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import psutil
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from corp_ed.core.security import create_access_token

API = "/api/v1"
CORPUS = Path(__file__).resolve().parents[2] / "tests/fixtures/stand_quality"
SUPPORTED = {".docx", ".doc", ".xlsx", ".pptx", ".pdf", ".txt", ".md", ".markdown"}


# --- токены и вопросы -----------------------------------------------------------


@dataclass(frozen=True)
class Person:
    account_id: UUID
    account_version: int
    member_id: UUID
    member_version: int
    role: str

    def headers(self, tenant_id: UUID) -> dict[str, str]:
        # Новый sid на каждый токен: записей refresh-токенов нет — сеанс
        # не закрыт (family_revoked), как в тестах API.
        token = create_access_token(
            self.account_id,
            self.account_version,
            session_id=uuid4(),
            tenant_id=tenant_id,
            member_id=self.member_id,
            role=self.role,
            member_version=self.member_version,
        )
        return {"Authorization": f"Bearer {token}"}


def _person(raw: dict[str, Any]) -> Person:
    return Person(
        UUID(raw["account_id"]),
        int(raw["account_version"]),
        UUID(raw["member_id"]),
        int(raw["member_version"]),
        str(raw["role"]),
    )


@dataclass
class Company:
    tenant_id: UUID
    admin: Person
    employees: list[Person]

    @classmethod
    def load(cls, path: Path) -> "Company":
        data = json.loads(path.read_text())
        return cls(
            UUID(data["tenant_id"]),
            _person(data["admin"]),
            [_person(raw) for raw in data["employees"]],
        )


def _questions() -> tuple[list[str], list[tuple[str, str]]]:
    """Отдельные вопросы и пары «вопрос — уточнение» корпуса."""
    cases = json.loads((CORPUS / "golden.json").read_text())
    single = [case["turns"][0]["q"] for case in cases]
    pairs = [
        (case["turns"][0]["q"], case["turns"][1]["q"])
        for case in cases
        if len(case["turns"]) > 1
    ]
    return single, pairs


# --- замеры ---------------------------------------------------------------------


@dataclass
class Sample:
    kind: str
    status: str  # "ok", HTTP-код или код события error
    total: float
    first_word: float | None = None


@dataclass
class Recorder:
    samples: list[Sample] = field(default_factory=list)

    def add(self, sample: Sample) -> None:
        self.samples.append(sample)

    def table(self, seconds: float) -> list[dict[str, Any]]:
        by_kind: dict[str, list[Sample]] = defaultdict(list)
        for sample in self.samples:
            by_kind[sample.kind].append(sample)
        rows = []
        for kind, samples in sorted(by_kind.items()):
            ok = [s for s in samples if s.status == "ok"]
            totals = sorted(s.total for s in ok)
            firsts = sorted(s.first_word for s in ok if s.first_word is not None)
            rows.append(
                {
                    "kind": kind,
                    "count": len(samples),
                    "per_s": round(len(ok) / seconds, 2),
                    "errors": dict(
                        Counter(s.status for s in samples if s.status != "ok")
                    ),
                    "p50": _pct(totals, 50),
                    "p95": _pct(totals, 95),
                    "max": round(totals[-1], 2) if totals else None,
                    "first_p50": _pct(firsts, 50),
                    "first_p95": _pct(firsts, 95),
                }
            )
        return rows


def _pct(values: list[float], pct: int) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 2)
    return round(statistics.quantiles(values, n=100, method="inclusive")[pct - 1], 2)


class Probe:
    """Процессор API и Postgres раз в секунду, соединения к базе."""

    def __init__(self, owner_url: str | None) -> None:
        self.api_cpu: list[float] = []
        self.db_cpu: list[float] = []
        self.connections: list[int] = []
        self._engine = create_async_engine(owner_url) if owner_url else None

    @staticmethod
    def _processes() -> tuple[list[psutil.Process], list[psutil.Process]]:
        api, db = [], []
        for proc in psutil.process_iter(["name", "cmdline"]):
            cmdline = " ".join(proc.info["cmdline"] or [])
            if "uvicorn" in cmdline and "corp_ed.main:app" in cmdline:
                api.append(proc)
            elif (proc.info["name"] or "").startswith("postgres"):
                db.append(proc)
        return api, db

    async def run(self, stop: asyncio.Event) -> None:
        api, db = self._processes()
        for proc in api + db:
            with contextlib.suppress(psutil.Error):
                proc.cpu_percent(None)
        while not stop.is_set():
            await asyncio.sleep(1.0)
            self.api_cpu.append(_cpu(api))
            # Новые соединения — новые процессы postgres.
            api_now, db = self._processes()
            self.db_cpu.append(_cpu(db))
            if self._engine is not None:
                async with self._engine.connect() as conn:
                    count = await conn.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname ="
                            " current_database() AND usename <> current_user"
                        )
                    )
                self.connections.append(int(count or 0))

    def summary(self) -> dict[str, Any]:
        return {
            "api_cpu_avg": _avg(self.api_cpu),
            "api_cpu_max": max(self.api_cpu, default=0.0),
            "db_cpu_avg": _avg(self.db_cpu),
            "db_cpu_max": max(self.db_cpu, default=0.0),
            "db_conn_max": max(self.connections, default=0),
        }

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()


def _cpu(processes: list[psutil.Process]) -> float:
    total = 0.0
    for proc in processes:
        with contextlib.suppress(psutil.Error):
            total += proc.cpu_percent(None)
    return round(total, 1)


def _avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 1) if values else 0.0


# --- запросы --------------------------------------------------------------------


@dataclass
class Asked:
    sample: Sample
    conversation_id: str | None = None
    answer_id: str | None = None


async def ask(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    question: str,
    *,
    kind: str,
    conversation_id: str | None = None,
    parent_id: str | None = None,
) -> Asked:
    if conversation_id is None:
        path, body = f"{API}/conversations", {"question": question}
    else:
        path = f"{API}/conversations/{conversation_id}/messages"
        body = {"question": question, "parent_id": parent_id}
    started = time.monotonic()
    first: float | None = None
    asked = Asked(Sample(kind, "no_done", 0.0))
    try:
        async with client.stream("POST", path, json=body, headers=headers) as response:
            if response.status_code != 200:
                await response.aread()
                asked.sample = Sample(kind, str(response.status_code), _since(started))
                return asked
            async for event in _events(response):
                kind_ = event.get("type")
                if kind_ == "start":
                    asked.conversation_id = event["conversation"]["id"]
                elif kind_ == "delta" and first is None:
                    first = _since(started)
                elif kind_ == "done":
                    asked.answer_id = event["answer"]["id"]
                    asked.sample = Sample(kind, "ok", _since(started), first)
                    return asked
                elif kind_ == "error":
                    asked.sample = Sample(kind, str(event.get("code")), _since(started))
                    return asked
    except httpx.HTTPError as exc:
        asked.sample = Sample(kind, type(exc).__name__, _since(started))
    return asked


async def _events(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    async for line in response.aiter_lines():
        if line.startswith("data: "):
            yield json.loads(line[6:])


async def get(
    client: httpx.AsyncClient, headers: dict[str, str], path: str, kind: str
) -> Sample:
    started = time.monotonic()
    try:
        response = await client.get(f"{API}{path}", headers=headers)
    except httpx.HTTPError as exc:
        return Sample(kind, type(exc).__name__, _since(started))
    status = "ok" if response.status_code == 200 else str(response.status_code)
    return Sample(kind, status, _since(started))


def _since(started: float) -> float:
    return round(time.monotonic() - started, 3)


# --- режимы ---------------------------------------------------------------------


async def open_app(
    client: httpx.AsyncClient, headers: dict[str, str], recorder: Recorder
) -> None:
    """Что грузит интерфейс при открытии — параллельно, как браузер."""
    paths = [
        "/auth/me",
        "/conversations",
        "/notifications",
        "/suggestions",
        "/onboarding",
    ]
    for sample in await asyncio.gather(
        *(get(client, headers, path, f"open {path}") for path in paths)
    ):
        recorder.add(sample)


async def employee_cycle(
    client: httpx.AsyncClient,
    company: Company,
    person: Person,
    recorder: Recorder,
    deadline: float,
    think: tuple[float, float],
) -> None:
    single, pairs = _questions()
    headers = person.headers(company.tenant_id)
    await asyncio.sleep(random.uniform(0, think[1]))  # не все открывают разом
    await open_app(client, headers, recorder)
    bell = asyncio.create_task(_bell(client, headers, recorder, deadline))
    try:
        while time.monotonic() < deadline:
            first_q, follow_q = (
                random.choice(pairs)
                if random.random() < 0.5
                else (
                    random.choice(single),
                    None,
                )
            )
            asked = await ask(client, headers, first_q, kind="вопрос")
            recorder.add(asked.sample)
            await asyncio.sleep(random.uniform(*think))
            if follow_q and asked.answer_id and time.monotonic() < deadline:
                follow = await ask(
                    client,
                    headers,
                    follow_q,
                    kind="уточнение",
                    conversation_id=asked.conversation_id,
                    parent_id=asked.answer_id,
                )
                recorder.add(follow.sample)
                await asyncio.sleep(random.uniform(*think))
    finally:
        bell.cancel()


async def _bell(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    recorder: Recorder,
    deadline: float,
) -> None:
    while time.monotonic() < deadline:
        await asyncio.sleep(60)
        recorder.add(await get(client, headers, "/notifications", "колокольчик"))


async def capacity_worker(
    client: httpx.AsyncClient,
    company: Company,
    pool: Iterator[Person],
    recorder: Recorder,
    deadline: float,
) -> None:
    single, _ = _questions()
    while time.monotonic() < deadline:
        headers = next(pool).headers(company.tenant_id)
        asked = await ask(client, headers, random.choice(single), kind="вопрос")
        recorder.add(asked.sample)


async def search_worker(
    client: httpx.AsyncClient,
    company: Company,
    pool: Iterator[Person],
    recorder: Recorder,
    deadline: float,
) -> None:
    single, _ = _questions()
    while time.monotonic() < deadline:
        headers = next(pool).headers(company.tenant_id)
        started = time.monotonic()
        try:
            response = await client.post(
                f"{API}/faq/search",
                json={"question": random.choice(single)},
                headers=headers,
            )
            status = "ok" if response.status_code == 200 else str(response.status_code)
        except httpx.HTTPError as exc:
            status = type(exc).__name__
        recorder.add(Sample("поиск", status, _since(started)))


async def level(
    args: argparse.Namespace, company: Company, users: int
) -> dict[str, Any]:
    recorder = Recorder()
    probe = Probe(os.environ.get("OWNER_DATABASE_URL"))
    stop = asyncio.Event()
    limits = httpx.Limits(max_connections=None, max_keepalive_connections=None)
    timeout = httpx.Timeout(args.timeout)
    pool = itertools.cycle(company.employees)
    started = time.monotonic()
    deadline = started + args.duration
    async with httpx.AsyncClient(
        base_url=args.base_url, limits=limits, timeout=timeout
    ) as client:
        probing = asyncio.create_task(probe.run(stop))
        if args.mode == "cycle":
            tasks = [
                employee_cycle(
                    client,
                    company,
                    company.employees[i],
                    recorder,
                    deadline,
                    args.think,
                )
                for i in range(users)
            ]
        elif args.mode == "capacity":
            tasks = [
                capacity_worker(client, company, pool, recorder, deadline)
                for _ in range(users)
            ]
        elif args.mode == "search":
            tasks = [
                search_worker(client, company, pool, recorder, deadline)
                for _ in range(users)
            ]
        else:  # burst
            single, _ = _questions()

            async def one(person: Person) -> None:
                headers = person.headers(company.tenant_id)
                asked = await ask(client, headers, random.choice(single), kind="вопрос")
                recorder.add(asked.sample)

            tasks = [one(company.employees[i]) for i in range(users)]
        await asyncio.gather(*tasks)
        stop.set()
        await probing
    await probe.close()
    seconds = time.monotonic() - started
    return {
        "mode": args.mode,
        "users": users,
        "seconds": round(seconds, 1),
        "rows": recorder.table(seconds),
        **probe.summary(),
    }


async def corpus(args: argparse.Namespace, company: Company) -> None:
    headers = company.admin.headers(company.tenant_id)
    files = sorted(
        path for path in (CORPUS / "docs").iterdir() if path.suffix.lower() in SUPPORTED
    )
    async with httpx.AsyncClient(base_url=args.base_url, timeout=120) as client:
        ids = []
        for path in files:
            response = await client.post(
                f"{API}/materials/upload",
                files={"file": (path.name, path.read_bytes())},
                data={"title": path.stem},
                headers=headers,
            )
            if response.status_code != 201:
                print(f"{path.name}: HTTP {response.status_code} {response.text[:200]}")
                continue
            ids.append(response.json()["id"])
        print(f"загружено {len(ids)} из {len(files)}, ждём индекс")
        while ids:
            await asyncio.sleep(2)
            statuses = []
            for material_id in ids:
                response = await client.get(
                    f"{API}/materials/{material_id}", headers=headers
                )
                statuses.append(response.json()["status"])
            pending = [s for s in statuses if s not in ("ready", "failed")]
            if not pending:
                print("готово:", dict(Counter(statuses)))
                return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "mode", choices=["corpus", "cycle", "capacity", "burst", "search"]
    )
    parser.add_argument("--users-file", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--levels", default="10", help="через запятую: 10,25,50")
    parser.add_argument("--duration", type=float, default=120.0)
    parser.add_argument("--think", type=float, nargs=2, default=(20.0, 40.0))
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--out", type=Path, help="JSON с результатами уровней")
    args = parser.parse_args()
    company = Company.load(args.users_file)
    if args.mode == "corpus":
        asyncio.run(corpus(args, company))
        return
    results = []
    for users in (int(n) for n in args.levels.split(",")):
        result = asyncio.run(level(args, company, users))
        results.append(result)
        print(json.dumps(result, ensure_ascii=False))
        if args.out:
            args.out.write_text(json.dumps(results, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
