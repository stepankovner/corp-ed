"""Возобновить прерываемый сервер стенда, если Selectel его остановил.

Прерываемый сервер Selectel останавливается в любой момент в течение
суток после запуска и сам не поднимается: в панели он в статусе EXPIRED,
в OpenStack — SHELVED или SHELVED_OFFLOADED, возобновление — unshelve
(docs.selectel.ru, «Восстановить прерываемый сервер»). Решение 30.09:
возобновлять автоматически — workflow «Stage resume» запускает этот
скрипт каждые 10 минут.

Только стандартная библиотека: запускается на раннере GitHub без установки
пакетов. Выключенный вручную сервер (SHUTOFF) не трогаем — его выключили
нарочно.

Переменные окружения (секреты — в GitHub, не в репозитории):
  SELECTEL_USER, SELECTEL_PASSWORD  сервисный пользователь
  SELECTEL_ACCOUNT_ID               номер аккаунта
  SELECTEL_PROJECT                  имя проекта, где только стенд
  SELECTEL_REGION                   пул, например ru-9
  STAGE_SERVER_ID                   id сервера (UUID)
"""

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

IDENTITY_URL = os.environ.get(
    "SELECTEL_IDENTITY_URL", "https://cloud.api.selcloud.ru/identity/v3/auth/tokens"
)
"""Переопределяется только для проверки на поддельном сервере."""
STOPPED_BY_PROVIDER = {"SHELVED", "SHELVED_OFFLOADED"}
TIMEOUT = 30


def _request(
    url: str, *, method: str = "GET", body: Any = None, token: str | None = None
) -> tuple[int, dict[str, str], Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)  # noqa: S310 — https из кода
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("X-Auth-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            raw = response.read()
            return (
                response.status,
                dict(response.headers),
                json.loads(raw) if raw else None,
            )
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
        raise SystemExit(f"токен Selectel: HTTP {status}")
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
    raise SystemExit(f"в каталоге нет compute для пула {region}")


def main() -> int:
    env = os.environ
    missing = [
        name
        for name in (
            "SELECTEL_USER",
            "SELECTEL_PASSWORD",
            "SELECTEL_ACCOUNT_ID",
            "SELECTEL_PROJECT",
            "SELECTEL_REGION",
            "STAGE_SERVER_ID",
        )
        if not env.get(name)
    ]
    if missing:
        print(f"не заданы: {', '.join(missing)}", file=sys.stderr)
        return 64

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
        print(f"сервер: HTTP {status}", file=sys.stderr)
        return 1
    state = payload["server"]["status"]
    print(f"статус сервера: {state}")
    if state not in STOPPED_BY_PROVIDER:
        return 0

    status, _, _ = _request(
        f"{server_url}/action", method="POST", body={"unshelve": None}, token=token
    )
    if status != 202:
        print(f"unshelve: HTTP {status}", file=sys.stderr)
        return 1
    print("сервер остановлен Selectel — отправлен на возобновление (unshelve)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
