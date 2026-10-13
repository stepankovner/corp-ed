"""Возобновить прерываемый сервер стенда и сообщить в Telegram, если он лежит.

Прерываемый сервер Selectel останавливается в любой момент в течение
суток после запуска и сам не поднимается: в панели он в статусе EXPIRED,
в OpenStack — SHELVED или SHELVED_OFFLOADED, возобновление — unshelve
(docs.selectel.ru, «Восстановить прерываемый сервер»). Решение 30.09:
возобновлять автоматически — workflow «Stage resume» запускает этот
скрипт каждые 10 минут. Решение 12.10: тот же запуск проверяет стенд
снаружи (/health/ready) и пишет в Telegram, когда стенд упал и когда
поднялся, — мониторинг на самом сервере молчит, если лежит сервер.

Только стандартная библиотека: запускается на раннере GitHub без установки
пакетов. Выключенный вручную сервер (SHUTOFF) не трогаем — его выключили
нарочно — и о нём не пишем.

Состояние между запусками (лежал ли стенд в прошлый раз) — в самом
workflow: задание «Stand down marker» выполняется, только когда стенд
лежит, и скрипт смотрит, выполнялось ли оно в прошлом запуске. Так
сообщение уходит один раз на смену состояния, а не каждые 10 минут.

Журнал запусков публичный: в вывод не попадают ни токен бота, ни чат.

Переменные окружения (секреты — в GitHub, не в репозитории). Каждая
группа необязательна: без неё эта часть пропускается.
  SELECTEL_USER, SELECTEL_PASSWORD  сервисный пользователь
  SELECTEL_ACCOUNT_ID               номер аккаунта
  SELECTEL_PROJECT                  имя проекта, где только стенд
  SELECTEL_REGION                   пул, например ru-7
  STAGE_SERVER_ID                   id сервера (UUID)
  STAGE_DOMAIN                      имя стенда для проверки снаружи
  TEAM_NOTIFY_TELEGRAM_BOT_TOKEN,
  TEAM_NOTIFY_TELEGRAM_CHAT_ID      бот и чат команды
  GITHUB_TOKEN, GITHUB_REPOSITORY,
  GITHUB_WORKFLOW_REF, GITHUB_RUN_ID,
  GITHUB_OUTPUT                     задаёт сам GitHub Actions
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

IDENTITY_URL = os.environ.get(
    "SELECTEL_IDENTITY_URL", "https://cloud.api.selcloud.ru/identity/v3/auth/tokens"
)
"""Переопределяется только для проверки на поддельном сервере."""
GITHUB_API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
TELEGRAM_API = "https://api.telegram.org"
STOPPED_BY_PROVIDER = {"SHELVED", "SHELVED_OFFLOADED"}
TIMEOUT = 30
MARKER_JOB = "Stand down marker"
"""Имя задания в stage-resume.yaml: выполнялось — стенд в том запуске лежал."""
PROBES = 3
PROBE_PAUSE = 20.0
"""Три попытки за ~40 с: перезапуск контейнеров на выкатке — не тревога."""
SELECTEL_VARS = (
    "SELECTEL_USER",
    "SELECTEL_PASSWORD",
    "SELECTEL_ACCOUNT_ID",
    "SELECTEL_PROJECT",
    "SELECTEL_REGION",
    "STAGE_SERVER_ID",
)
DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")


def _request(
    url: str,
    *,
    method: str = "GET",
    body: Any = None,
    token: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, str], Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)  # noqa: S310 — https из кода
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("X-Auth-Token", token)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            raw = response.read()
            try:
                payload = json.loads(raw) if raw else None
            except ValueError:
                payload = None
            return response.status, dict(response.headers), payload
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None
        return exc.code, dict(exc.headers), payload


def project_token(
    user: str, password: str, account: str, project: str
) -> tuple[str, Any]:
    """IAM-токен проекта (X-Subject-Token) и каталог адресов сервисов."""
    body = {
        "auth": {
            "identity": {
                "methods": ["password"],
                "password": {
                    "user": {
                        "name": user,
                        "domain": {"name": account},
                        "password": password,
                    }
                },
            },
            "scope": {"project": {"name": project, "domain": {"name": account}}},
        }
    }
    status, headers, payload = _request(IDENTITY_URL, method="POST", body=body)
    token = headers.get("X-Subject-Token") or headers.get("x-subject-token")
    if status != 201 or not token:
        raise RuntimeError(f"токен Selectel: HTTP {status}")
    return token, payload["token"]["catalog"]


def compute_url(catalog: Any, region: str) -> str:
    for service in catalog:
        if service.get("type") != "compute":
            continue
        for endpoint in service.get("endpoints", []):
            if (
                endpoint.get("interface") == "public"
                and endpoint.get("region") == region
            ):
                return str(endpoint["url"]).rstrip("/")
    raise RuntimeError(f"в каталоге нет compute для пула {region}")


def resume_server(env: Mapping[str, str]) -> tuple[str, bool]:
    """Статус сервера и признак «отправлен на возобновление»."""
    token, catalog = project_token(
        env["SELECTEL_USER"],
        env["SELECTEL_PASSWORD"],
        env["SELECTEL_ACCOUNT_ID"],
        env["SELECTEL_PROJECT"],
    )
    compute = compute_url(catalog, env["SELECTEL_REGION"])
    server_url = f"{compute}/servers/{env['STAGE_SERVER_ID']}"
    status, _, payload = _request(server_url, token=token)
    if status != 200:
        raise RuntimeError(f"сервер: HTTP {status}")
    state = str(payload["server"]["status"])
    print(f"статус сервера: {state}")
    if state not in STOPPED_BY_PROVIDER:
        return state, False

    status, _, _ = _request(
        f"{server_url}/action", method="POST", body={"unshelve": None}, token=token
    )
    if status != 202:
        raise RuntimeError(f"unshelve: HTTP {status}")
    print("сервер остановлен Selectel — отправлен на возобновление (unshelve)")
    return state, True


def probe(
    domain: str,
    *,
    attempts: int = PROBES,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """None — стенд отвечает; иначе — что пошло не так (последняя попытка)."""
    problem = "нет ответа"
    for attempt in range(attempts):
        if attempt:
            sleep(PROBE_PAUSE)
        try:
            status, _, _ = _request(f"https://{domain}/health/ready")
        except (urllib.error.URLError, TimeoutError, OSError):
            problem = "нет соединения"
            continue
        if status == 200:
            return None
        problem = f"HTTP {status}"
    return problem


def previous_down(env: Mapping[str, str]) -> bool | None:
    """Лежал ли стенд в прошлом завершённом запуске; None — неизвестно."""
    token = env.get("GITHUB_TOKEN")
    repo = env.get("GITHUB_REPOSITORY")
    ref = env.get("GITHUB_WORKFLOW_REF", "")
    run_id = env.get("GITHUB_RUN_ID", "")
    # owner/repo/.github/workflows/stage-resume.yaml@refs/heads/main
    workflow = ref.split("@", 1)[0].rsplit("/", 1)[-1]
    if not (token and repo and workflow):
        return None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    try:
        status, _, runs = _request(
            f"{GITHUB_API}/repos/{repo}/actions/workflows/{workflow}/runs"
            "?status=completed&per_page=5",
            headers=headers,
        )
        if status != 200:
            print(f"прошлый запуск: HTTP {status}", file=sys.stderr)
            return None
        previous = next(
            (
                run
                for run in runs.get("workflow_runs", [])
                if str(run.get("id")) != run_id
            ),
            None,
        )
        if previous is None:
            return None
        status, _, jobs = _request(
            f"{GITHUB_API}/repos/{repo}/actions/runs/{previous['id']}/jobs",
            headers=headers,
        )
    except (urllib.error.URLError, TimeoutError, OSError):
        print("прошлый запуск: нет соединения с GitHub", file=sys.stderr)
        return None
    if status != 200:
        print(f"задания прошлого запуска: HTTP {status}", file=sys.stderr)
        return None
    for job in jobs.get("jobs", []):
        if job.get("name") == MARKER_JOB:
            if job.get("conclusion") == "success":
                return True
            if job.get("conclusion") == "skipped":
                return False
    return None


def message(
    *,
    domain: str,
    down: bool,
    was_down: bool | None,
    resumed: bool,
    problem: str | None,
) -> str | None:
    """Текст для Telegram или None, если состояние не поменялось."""
    name = domain or "стенд"
    if resumed and was_down is not True:
        return (
            f"🔴 {name}: Selectel остановил прерываемый сервер — "
            "отправлен на возобновление, обычно это 5–15 минут."
        )
    if down and was_down is not True:
        return f"🔴 {name} не отвечает: /health/ready — {problem or 'нет ответа'}."
    if not down and was_down is True:
        return f"🟢 {name} снова работает."
    return None


def notify(token: str, chat_id: str, text: str) -> None:
    try:
        status, _, _ = _request(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            method="POST",
            body={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        )
    except (urllib.error.URLError, TimeoutError, OSError):
        print("Telegram: нет соединения", file=sys.stderr)
        return
    if status == 200:
        print("сообщение в Telegram отправлено")
    else:
        print(f"Telegram: HTTP {status}", file=sys.stderr)


def main(env: Mapping[str, str] = os.environ) -> int:
    failed = False
    domain = env.get("STAGE_DOMAIN", "").strip()
    if domain and not DOMAIN_RE.match(domain):
        print("STAGE_DOMAIN — не имя хоста", file=sys.stderr)
        return 64

    # 1. Selectel: возобновить, если остановил он.
    state: str | None = None
    resumed = False
    present = [name for name in SELECTEL_VARS if env.get(name)]
    if present and len(present) < len(SELECTEL_VARS):
        missing = [name for name in SELECTEL_VARS if not env.get(name)]
        print(f"не заданы: {', '.join(missing)}", file=sys.stderr)
        failed = True
    elif present:
        try:
            state, resumed = resume_server(env)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            failed = True

    was_down = previous_down(env)

    # 2. Снаружи. Выключенный вручную сервер — не тревога: состояние то же.
    problem: str | None = None
    if state == "SHUTOFF":
        print("сервер выключен вручную — не проверяю и не пишу")
        down = bool(was_down)
    elif resumed or (state is not None and state != "ACTIVE"):
        down = True
        problem = f"сервер в статусе {state}"
    elif domain:
        problem = probe(domain)
        down = problem is not None
        print("стенд отвечает" if not down else f"стенд не отвечает: {problem}")
    else:
        down = False

    # 3. Telegram — только на смену состояния.
    text = message(
        domain=domain, down=down, was_down=was_down, resumed=resumed, problem=problem
    )
    token = env.get("TEAM_NOTIFY_TELEGRAM_BOT_TOKEN")
    chat_id = env.get("TEAM_NOTIFY_TELEGRAM_CHAT_ID")
    if text and token and chat_id:
        notify(token, chat_id, text)
    elif text:
        print("состояние сменилось, но Telegram не настроен")

    output = env.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as file:
            file.write(f"down={'true' if down else 'false'}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
