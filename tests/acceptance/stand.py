"""Docker-стенды приёмки: поднять и убрать.

    python -m tests.acceptance.stand up bookstack
    python -m tests.acceptance.stand down bookstack
    python -m tests.acceptance.stand list

Compose-файлы — те же, что у живых проверок (`tests/live/<вид>/`), они не
меняются. Проект compose называется `kronto-acc-<система>` (ключ `-p`
перекрывает `name:` файла), порт — из диапазона 8520–8539 через
переменную порта самого файла. Docker общий: больше двух стендов
одновременно не поднимать, после проверки — `down`, затем `docker image rm`.

Сертификат TLS-прокси стенда — `~/.kronto-acc/tls` (или ACC_TLS_DIR),
создаётся при первом `up` тем же кодом, что в tests/live/dav_stand.py.
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
PROJECT_PREFIX = "kronto-acc-"
DEFAULT_TLS = Path("~/.kronto-acc/tls").expanduser()


@dataclass(frozen=True)
class Stand:
    system: str
    compose: str
    port_env: str
    port: int
    ready_path: str = "/"
    env: dict[str, str] = field(default_factory=dict)
    wait_seconds: int = 300

    @property
    def project(self) -> str:
        return PROJECT_PREFIX + self.system

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}/"


STANDS: dict[str, Stand] = {
    stand.system: stand
    for stand in (
        Stand(
            "bookstack",
            "tests/live/bookstack/compose.yaml",
            "BOOKSTACK_PORT",
            8520,
            "/login",
        ),
        Stand(
            "nextcloud",
            "tests/live/nextcloud/compose.yaml",
            "NEXTCLOUD_PORT",
            8521,
            "/status.php",
            {"POSTGRES_VERSION": "15-alpine"},
        ),
        Stand(
            "owncloud",
            "tests/live/owncloud/compose.yaml",
            "OWNCLOUD_PORT",
            8522,
            "/status.php",
            {"POSTGRES_VERSION": "15-alpine"},
        ),
        Stand(
            "dokuwiki", "tests/live/dokuwiki/compose.yaml", "DOKUWIKI_PORT", 8529, "/"
        ),
    )
}


def stand_url(system: str) -> str:
    """Адрес стенда системы: ACC_<СИСТЕМА>_URL или порт из таблицы выше."""
    override = os.environ.get(f"ACC_{system.upper()}_URL")
    if override:
        return override
    if system not in STANDS:
        raise SystemExit(f"для {system} нет Docker-стенда приёмки")
    return STANDS[system].url


def tls_dir() -> Path:
    return Path(os.environ.get("ACC_TLS_DIR", str(DEFAULT_TLS))).expanduser()


def ensure_tls() -> Path:
    """Сертификат стенда на 127.0.0.1; создаётся один раз."""
    directory = tls_dir()
    if not (directory / "cert.pem").exists():
        from tests.live.dav_stand import write_tls

        write_tls(directory)
    return directory


def _docker() -> str:
    found = shutil.which("docker")
    if found is None:
        raise SystemExit("docker не найден в PATH")
    return found


def compose_args(stand: Stand, *args: str) -> list[str]:
    return [
        _docker(),
        "compose",
        "-p",
        stand.project,
        "-f",
        str(ROOT / stand.compose),
        *args,
    ]


def compose_env(stand: Stand) -> dict[str, str]:
    return {
        **os.environ,
        **stand.env,
        stand.port_env: str(stand.port),
        "DAV_TLS_DIR": str(ensure_tls()),
    }


def up(stand: Stand) -> None:
    subprocess.run(
        compose_args(stand, "up", "-d"), env=compose_env(stand), check=True, cwd=ROOT
    )
    wait_ready(stand)


def down(stand: Stand) -> None:
    subprocess.run(
        compose_args(stand, "down", "-v"), env=compose_env(stand), check=False, cwd=ROOT
    )


def exec_in(
    stand: Stand,
    service: str,
    *command: str,
    user: str | None = None,
    input_text: str | None = None,
) -> str:
    """Выполнить команду в контейнере стенда (`docker compose exec`);
    input_text — на стандартный ввод команды."""
    args = ["exec", "-T"]
    if user:
        args += ["-u", user]
    result = subprocess.run(
        compose_args(stand, *args, service, *command),
        env=compose_env(stand),
        check=True,
        cwd=ROOT,
        capture_output=True,
        text=True,
        input=input_text,
    )
    return result.stdout


def wait_ready(stand: Stand) -> None:
    deadline = time.monotonic() + stand.wait_seconds
    cert = str(tls_dir() / "cert.pem")
    last = ""
    while time.monotonic() < deadline:
        try:
            response = httpx.get(
                stand.url.rstrip("/") + stand.ready_path, verify=cert, timeout=10
            )
            last = f"HTTP {response.status_code}"
            if response.status_code == 200 and _installed(stand, response):
                return
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        time.sleep(5)
    raise SystemExit(
        f"стенд {stand.system} не поднялся за {stand.wait_seconds} с: {last}"
    )


def _installed(stand: Stand, response: httpx.Response) -> bool:
    if stand.system in ("nextcloud", "owncloud"):
        return bool(response.json().get("installed"))
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.acceptance.stand")
    parser.add_argument("command", choices=("up", "down", "list"))
    parser.add_argument("system", nargs="?")
    args = parser.parse_args(argv)
    if args.command == "list":
        for stand in STANDS.values():
            print(f"{stand.system:10} порт {stand.port}  проект {stand.project}")
        return 0
    if args.system not in STANDS:
        parser.error(f"стенд есть для: {', '.join(STANDS)}")
    stand = STANDS[args.system]
    if args.command == "up":
        up(stand)
        print(f"{stand.system}: {stand.url}  (сертификат: {tls_dir()}/cert.pem)")
    else:
        down(stand)
    return 0


if __name__ == "__main__":
    sys.exit(main())
