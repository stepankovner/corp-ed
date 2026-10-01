"""Проверка стенда через HTTP API — тем же путём, каким ходит фронт.

    CORP_ED_BASE_URL=https://api.example.ru CORP_ED_COMPANY=demo \\
    CORP_ED_EMAIL=admin@demo.ru CORP_ED_PASSWORD=… \\
        python -m corp_ed.stand check
    python -m corp_ed.stand upload --dir ./demo-corpus [--titles titles.json]

`check` — сквозной сценарий этапа 5 (WORKLOG): вход администратора,
загрузка небольшого документа, ожидание индексации воркером, вопрос по
документу (ответ по документам со ссылкой на него), оценка ответа,
уточняющий вопрос в том же диалоге (память диалога, BH-28), вопрос вне
документов (общий ответ с пометкой или отказ — по режиму компании),
расход кредитов, удаление документа. Каждый шаг печатается с итогом;
код выхода 1, если хоть один не прошёл. Документ создаётся с уникальным
кодовым словом, поэтому повторные запуски не конфликтуют и не зависят
от того, что уже загружено в компанию.

`upload` — загрузить папку документов (docx, doc, xlsx, pptx, pdf, txt,
md) в компанию:
демо-корпус для стенда ML (backend-handoff v2, раздел 3). Названия — из
JSON «имя файла → название» или по имени файла.

Переменные — те же, что у клиента eval ML (`eval/api_client.py`):
CORP_ED_BASE_URL, CORP_ED_COMPANY, CORP_ED_EMAIL, CORP_ED_PASSWORD или
CORP_ED_TOKEN. Пароль не принимается аргументом, чтобы не оседал в
истории shell. Для первого входа с временным паролем —
CORP_ED_NEW_PASSWORD: сценарий сменит пароль и продолжит.

Модуль не читает настройки приложения и не ходит в базу: его можно
запускать с любой машины, у которой есть доступ к API.
"""

import argparse
import asyncio
import json
import os
import secrets
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

API = "/api/v1"
DEFAULT_BASE_URL = "http://localhost:8000"
# Что сервер принимает при всех включённых форматах Р-5 (INGEST_EXTRA_FORMATS);
# выключенный формат сервер отклонит с подсказкой — шаг покажет это.
SUPPORTED_SUFFIXES = (".docx", ".doc", ".xlsx", ".pptx", ".pdf", ".txt", ".md")
INDEX_TIMEOUT = 300.0
POLL_INTERVAL = 2.0
# Вопрос, на который в документах компании ответа быть не может.
OUTSIDE_QUESTION = "Какая сейчас температура на поверхности Венеры в градусах Цельсия?"
FOLLOW_UP_QUESTION = "А кто его называет?"
"""Уточнение к вопросу про кодовое слово: понятно только в диалоге (BH-28)."""


class StandError(Exception):
    """Шаг не может продолжаться: дальше проверять нечего."""


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    steps: list[Step] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(step.ok for step in self.steps)

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.steps.append(Step(name, ok, detail))
        return ok

    def lines(self) -> list[str]:
        return [
            f"{'OK ' if step.ok else 'FAIL'} {step.name}"
            + (f" — {step.detail}" if step.detail else "")
            for step in self.steps
        ]


def smoke_document(nonce: str) -> tuple[str, bytes, str, str]:
    """Имя файла, содержимое, название и вопрос для проверки.

    Кодовое слово — случайное: модель не может знать его заранее, так что
    правильный ответ возможен только по документу, а не из общих знаний.
    """
    text = (
        f"# Регламент проверки стенда {nonce}\n\n"
        "## Кодовое слово\n\n"
        f"Кодовое слово проверки стенда — {nonce}. Его называет дежурный "
        "инженер при приёмке новой сборки.\n\n"
        "## Дежурство\n\n"
        "Дежурный инженер принимает сборку по средам с 10 до 12 часов "
        "по московскому времени.\n"
    )
    return (
        f"stand-check-{nonce}.md",
        text.encode("utf-8"),
        f"Регламент проверки стенда {nonce}",
        "Какое кодовое слово проверки стенда указано в регламенте?",
    )


class StandClient:
    """Тонкая обёртка над API: токен, пути, понятные ошибки."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http
        self.token: str | None = None

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = path if path.startswith("/health") else f"{API}{path}"
        return await self.http.request(method, url, headers=self._headers(), **kwargs)

    async def login(self, company: str, email: str, password: str) -> None:
        response = await self.request(
            "POST",
            "/auth/login",
            json={"company_code": company, "email": email, "password": password},
        )
        if response.status_code != 200:
            raise StandError(f"вход: HTTP {response.status_code} {_code(response)}")
        self.token = response.json()["access_token"]

    async def change_password(self, current: str, new: str) -> None:
        response = await self.request(
            "POST",
            "/auth/change-password",
            json={"current_password": current, "new_password": new},
        )
        if response.status_code != 200:
            raise StandError(
                f"смена пароля: HTTP {response.status_code} {_code(response)}"
            )
        self.token = response.json()["access_token"]

    async def upload(self, filename: str, data: bytes, title: str) -> dict[str, Any]:
        response = await self.request(
            "POST",
            "/materials/upload",
            files={"file": (filename, data)},
            data={"title": title},
        )
        if response.status_code != 201:
            raise StandError(
                f"загрузка {filename}: HTTP {response.status_code} {_code(response)}"
            )
        material: dict[str, Any] = response.json()
        return material


def _code(response: httpx.Response) -> str:
    """Код ошибки API без тела целиком: в ответ может попасть лишнее."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        for key in ("code", "error_code", "detail"):
            value = body.get(key)
            if isinstance(value, str):
                return value[:120]
    return ""


Hook = Callable[[], Awaitable[None]]


async def _wait_ready(
    client: StandClient,
    material_id: str,
    *,
    timeout: float,
    poll_interval: float,
    before_poll: Hook | None,
    sleep: Callable[[float], Awaitable[None]],
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        if before_poll is not None:
            await before_poll()
        response = await client.request("GET", f"/materials/{material_id}")
        if response.status_code != 200:
            raise StandError(f"статус материала: HTTP {response.status_code}")
        material: dict[str, Any] = response.json()
        if material["status"] in ("ready", "failed"):
            return material
        if time.monotonic() >= deadline:
            raise StandError(
                f"индексация не закончилась за {timeout:.0f} с "
                f"(статус {material['status']}; воркер запущен?)"
            )
        await sleep(poll_interval)


async def run_check(
    http: httpx.AsyncClient,
    *,
    company: str,
    email: str,
    password: str | None = None,
    token: str | None = None,
    new_password: str | None = None,
    nonce: str | None = None,
    timeout: float = INDEX_TIMEOUT,
    poll_interval: float = POLL_INTERVAL,
    before_poll: Hook | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Report:
    """Сквозной сценарий против API. before_poll — для тестов: запустить
    воркер индексации в том же процессе перед очередным опросом статуса."""
    report = Report()
    client = StandClient(http)
    nonce = nonce or secrets.token_hex(4)
    material_id: str | None = None
    try:
        health = await client.request("GET", "/health")
        report.add("health", health.status_code == 200, f"HTTP {health.status_code}")

        if token:
            client.token = token
        elif password:
            await client.login(company, email, password)
        else:
            raise StandError("нужен CORP_ED_PASSWORD или CORP_ED_TOKEN")
        me = (await client.request("GET", "/auth/me")).json()
        if me.get("must_change_password"):
            if not (new_password and password):
                raise StandError(
                    "временный пароль: задайте CORP_ED_NEW_PASSWORD для первой смены"
                )
            await client.change_password(password, new_password)
            me = (await client.request("GET", "/auth/me")).json()
        report.add(
            "вход администратора",
            me.get("role") == "admin",
            f"роль {me.get('role')}, компания {me.get('company_name')}",
        )

        usage_before = (await client.request("GET", "/usage")).json()

        filename, data, title, question = smoke_document(nonce)
        material = await client.upload(filename, data, title)
        material_id = str(material["id"])
        report.add("загрузка документа", True, f"{filename}, id {material_id}")

        started = time.monotonic()
        material = await _wait_ready(
            client,
            material_id,
            timeout=timeout,
            poll_interval=poll_interval,
            before_poll=before_poll,
            sleep=sleep,
        )
        report.add(
            "индексация воркером",
            material["status"] == "ready",
            f"{material['status']} за {time.monotonic() - started:.0f} с"
            + (f", {material['status_error']}" if material.get("status_error") else ""),
        )

        answer = await _ask(client, question)
        sources = answer.get("sources", [])
        cited = any(str(s.get("material_id")) == material_id for s in sources)
        named = nonce in answer.get("content", "")
        report.add(
            "ответ по документу",
            answer.get("origin") == "documents"
            and bool(answer.get("answer_given"))
            and cited
            and named,
            f"origin {answer.get('origin')}, источников {len(sources)}, "
            f"документ среди источников: {_yes(cited)}, "
            f"кодовое слово в ответе: {_yes(named)}",
        )
        if answer.get("answer_id"):
            rated = await client.request(
                "PATCH", f"/faq/answers/{answer['answer_id']}", json={"value": 1}
            )
            report.add(
                "оценка ответа", rated.status_code == 204, f"HTTP {rated.status_code}"
            )

        await _check_follow_up(client, answer, report)

        outside = await _ask(client, OUTSIDE_QUESTION)
        outside_sources = outside.get("sources", [])
        report.add(
            "вопрос вне документов",
            outside.get("origin") in ("general_knowledge", "none")
            and not outside_sources,
            f"origin {outside.get('origin')}, источников {len(outside_sources)}",
        )

        usage_after = (await client.request("GET", "/usage")).json()
        spent = usage_after["used"] - usage_before["used"]
        report.add(
            "расход кредитов",
            spent >= 1 and not usage_after["exhausted"],
            f"списано {spent}, осталось {usage_after['remaining']} "
            f"из {usage_after['pool']}",
        )
    except StandError as exc:
        report.add("сценарий прерван", False, str(exc))
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        report.add("сценарий прерван", False, f"{type(exc).__name__}: {exc}"[:200])
    finally:
        if material_id is not None and client.token:
            deleted = await client.request("DELETE", f"/materials/{material_id}")
            report.add(
                "удаление документа",
                deleted.status_code == 204,
                f"HTTP {deleted.status_code}",
            )
    return report


def _yes(value: bool) -> str:
    return "да" if value else "нет"


async def _check_follow_up(
    client: StandClient, previous: dict[str, Any], report: Report
) -> None:
    """Уточняющий вопрос в том же диалоге (BH-28).

    Диалог должен продолжиться (тот же conversation_id). Если память
    включена на сервере (RAG_HISTORY_TURNS > 0), «А кто его называет?»
    должен пониматься как вопрос про кодовое слово: ответ — по документу,
    про дежурного инженера. Выключена — шаг это только сообщает.
    """
    conversation = previous.get("conversation_id")
    follow = await _ask(client, FOLLOW_UP_QUESTION, conversation_id=conversation)
    same = bool(conversation) and follow.get("conversation_id") == conversation
    diagnostics = follow.get("diagnostics") or {}
    turns = int(diagnostics.get("history_turns") or 0)
    if not turns:
        report.add(
            "уточняющий вопрос",
            same,
            f"диалог продолжен: {_yes(same)}; память диалога на сервере "
            "выключена (RAG_HISTORY_TURNS=0)",
        )
        return
    about_duty = "дежурн" in str(follow.get("content", "")).casefold()
    report.add(
        "уточняющий вопрос",
        same and follow.get("origin") == "documents" and about_duty,
        f"диалог продолжен: {_yes(same)}, учтено реплик {turns}, "
        f"понят как «{diagnostics.get('standalone_question')}», "
        f"origin {follow.get('origin')}, ответ про дежурного: {_yes(about_duty)}",
    )


async def _ask(
    client: StandClient, question: str, *, conversation_id: str | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"question": question}
    if conversation_id:
        body["conversation_id"] = conversation_id
    response = await client.request("POST", "/faq/ask", json=body)
    if response.status_code != 200:
        raise StandError(f"вопрос: HTTP {response.status_code} {_code(response)}")
    answer: dict[str, Any] = response.json()
    return answer


async def upload_directory(
    http: httpx.AsyncClient,
    directory: Path,
    *,
    company: str,
    email: str,
    password: str | None = None,
    token: str | None = None,
    titles: dict[str, str] | None = None,
) -> Report:
    """Загрузить все поддерживаемые файлы папки. Дубликат (тот же sha256
    уже есть в компании) — не ошибка: повторный запуск ничего не ломает."""
    report = Report()
    client = StandClient(http)
    if token:
        client.token = token
    elif password:
        await client.login(company, email, password)
    else:
        raise StandError("нужен CORP_ED_PASSWORD или CORP_ED_TOKEN")
    files = sorted(
        p
        for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
    )
    if not files:
        report.add("файлы", False, f"в {directory} нет {', '.join(SUPPORTED_SUFFIXES)}")
    for path in files:
        title = (titles or {}).get(path.name) or path.stem.replace("_", " ")
        response = await client.request(
            "POST",
            "/materials/upload",
            files={"file": (path.name, path.read_bytes())},
            data={"title": title},
        )
        if response.status_code == 201:
            report.add(path.name, True, f"«{title}», id {response.json()['id']}")
        elif response.status_code == 409:
            report.add(path.name, True, "уже загружен")
        else:
            report.add(
                path.name, False, f"HTTP {response.status_code} {_code(response)}"
            )
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m corp_ed.stand",
        description="Проверка стенда через HTTP API (переменные CORP_ED_*).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="сквозной сценарий: документ → ответ")
    check.add_argument("--timeout", type=float, default=INDEX_TIMEOUT)
    upload = commands.add_parser("upload", help="загрузить папку документов")
    upload.add_argument("--dir", required=True, type=Path)
    upload.add_argument(
        "--titles", type=Path, help="JSON: имя файла → название документа"
    )
    return parser


async def _main(args: argparse.Namespace) -> int:
    env = os.environ
    base_url = env.get("CORP_ED_BASE_URL", DEFAULT_BASE_URL)
    company = env.get("CORP_ED_COMPANY", "")
    email = env.get("CORP_ED_EMAIL", "")
    token = env.get("CORP_ED_TOKEN") or None
    if not token and not (company and email):
        print(
            "Ошибка: задайте CORP_ED_COMPANY и CORP_ED_EMAIL (или CORP_ED_TOKEN)",
            file=sys.stderr,
        )
        return 2
    async with httpx.AsyncClient(base_url=base_url, timeout=120.0) as http:
        if args.command == "check":
            report = await run_check(
                http,
                company=company,
                email=email,
                password=env.get("CORP_ED_PASSWORD") or None,
                token=token,
                new_password=env.get("CORP_ED_NEW_PASSWORD") or None,
                timeout=args.timeout,
            )
        else:
            titles = json.loads(args.titles.read_text("utf-8")) if args.titles else None
            try:
                report = await upload_directory(
                    http,
                    args.dir,
                    company=company,
                    email=email,
                    password=env.get("CORP_ED_PASSWORD") or None,
                    token=token,
                    titles=titles,
                )
            except StandError as exc:
                print(f"Ошибка: {exc}", file=sys.stderr)
                return 1
    print(f"Стенд: {base_url}")
    for line in report.lines():
        print(line)
    print("Итог: " + ("все шаги прошли" if report.ok else "есть ошибки"))
    return 0 if report.ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_main(_parser().parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
