"""Командная строка агента.

    kronto-agent run   --config /etc/kronto-agent/agent.toml [--once]
    kronto-agent check --config /etc/kronto-agent/agent.toml

run — обход раз в interval_minutes (или один раз с --once); SIGTERM/SIGINT
прерывают обход между файлами (finish с complete=false). check —
проверка настроек: LDAP, каждый ресурс SMB, ключ агента в kronto.
"""

import argparse
import logging
import signal
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType

from kronto_agent import __version__
from kronto_agent.config import AgentConfig, ConfigError, ResourceConfig, load_config
from kronto_agent.directory import AdDirectory, DirectoryError, ldap3_search
from kronto_agent.kronto import (
    KrontoAuthError,
    KrontoClient,
    KrontoError,
    KrontoForbiddenError,
)
from kronto_agent.smb import SmbError, SmbResource, WalkReport
from kronto_agent.sync import Syncer

log = logging.getLogger("kronto_agent")


@contextmanager
def open_directory(cfg: AgentConfig) -> Iterator[AdDirectory]:
    search, close = ldap3_search(cfg.ldap)
    try:
        yield AdDirectory(
            search,
            cfg.ldap.base_dn,
            upn_fallback=cfg.upn_fallback,
            local_groups=cfg.local_groups,
        )
    finally:
        close()


def kronto_client(cfg: AgentConfig) -> KrontoClient:
    return KrontoClient(cfg.kronto.url, cfg.kronto.key, ca_file=cfg.kronto.ca_file)


def run_once(cfg: AgentConfig, stop: threading.Event) -> bool:
    """Один обход; False — обход не удался (подробности в журнале)."""
    kronto = kronto_client(cfg)
    try:
        with open_directory(cfg) as directory:
            report = Syncer(cfg, kronto, directory, _factory, stop).run()
        return report.complete
    except (KrontoAuthError, KrontoForbiddenError) as exc:
        log.error("%s", exc)
        return False
    except (KrontoError, DirectoryError) as exc:
        log.error("обход не выполнен: %s", exc)
        return False
    finally:
        kronto.close()


def _factory(cfg: ResourceConfig) -> SmbResource:
    return SmbResource(cfg)


def cmd_run(cfg: AgentConfig, once: bool) -> int:
    stop = threading.Event()

    def _stop(signum: int, _frame: FrameType | None) -> None:
        log.info("сигнал %d: останавливаюсь после текущего файла", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while True:
        ok = run_once(cfg, stop)
        if once:
            return 0 if ok else 1
        if stop.wait(cfg.interval_minutes * 60):
            return 0


def cmd_check(cfg: AgentConfig) -> int:
    ok = True
    try:
        with open_directory(cfg) as directory:
            with_mail = sum(1 for u in directory.users.values() if u.email)
            print(
                f"LDAP: ok, домен {directory.domain_sid}, включённых пользователей "
                f"{len(directory.users)}, с почтой {with_mail}"
            )
    except DirectoryError as exc:
        print(f"LDAP: ошибка: {exc}")
        ok = False
    for res in cfg.resources:
        try:
            with SmbResource(res) as resource:
                report = WalkReport()
                first = next(iter(resource.walk(cfg.exclude, report)), None)
                if report.errors:
                    raise SmbError("; ".join(report.errors))
                if first is not None:
                    resource.security_descriptor(first.unc)
                print(
                    f"SMB {res.unc}: ok"
                    + (f", первый файл {first.rel}" if first else "")
                )
        except Exception as exc:
            print(f"SMB {res.unc}: ошибка: {exc}")
            ok = False
    kronto = kronto_client(cfg)
    try:
        kronto.state()
        print(f"kronto {cfg.kronto.url}: ok")
    except KrontoError as exc:
        print(f"kronto {cfg.kronto.url}: ошибка: {exc}")
        ok = False
    finally:
        kronto.close()
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kronto-agent", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="обходить ресурсы по расписанию")
    run_p.add_argument("--config", required=True)
    run_p.add_argument("--once", action="store_true", help="один обход и выход")
    check_p = sub.add_parser("check", help="проверить настройки и доступы")
    check_p.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # Сообщения smbprotocol на INFO — по строке на каждый запрос SMB.
    logging.getLogger("smbprotocol").setLevel(logging.WARNING)
    logging.getLogger("smbclient").setLevel(logging.WARNING)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"настройки: {exc}", file=sys.stderr)
        return 2
    if args.command == "check":
        return cmd_check(cfg)
    return cmd_run(cfg, once=args.once)
