"""Настройки агента: файл TOML (пример — README.md).

Секреты (ключ агента, пароли LDAP и ресурсов) в файле открытым текстом не
принимаются: только `*_env` (имя переменной окружения) или `*_file` (путь
к файлу, например секрет Docker). Читаются при загрузке — ошибка видна
сразу при запуске, а не посреди обхода.
"""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DEFAULT_EXCLUDE = ("~$*", "~*.tmp", "*.tmp", "Thumbs.db", "desktop.ini", ".*")


class ConfigError(ValueError):
    """Ошибка в файле настроек."""


@dataclass(frozen=True)
class KrontoConfig:
    url: str
    key: str = field(repr=False)
    ca_file: str | None = None


@dataclass(frozen=True)
class LdapConfig:
    host: str
    port: int
    use_ssl: bool
    start_tls: bool
    base_dn: str
    bind_dn: str
    password: str = field(repr=False)
    ca_file: str | None = None


@dataclass(frozen=True)
class ResourceConfig:
    server: str
    share: str
    # Путь внутри ресурса без ведущего «\» ("" — весь ресурс).
    subpath: str
    username: str
    password: str = field(repr=False)
    port: int = 445
    encrypt: bool = False
    share_readers: str | None = None
    exclude: tuple[str, ...] = ()

    @property
    def unc(self) -> str:
        base = rf"\\{self.server}\{self.share}"
        return rf"{base}\{self.subpath}" if self.subpath else base


@dataclass(frozen=True)
class AgentConfig:
    kronto: KrontoConfig
    ldap: LdapConfig
    resources: tuple[ResourceConfig, ...]
    interval_minutes: int = 60
    max_file_bytes: int = 50 * 1024 * 1024
    exclude: tuple[str, ...] = DEFAULT_EXCLUDE
    upn_fallback: bool = False
    # SID локальной группы файлового сервера → имена/SID групп домена.
    local_groups: dict[str, tuple[str, ...]] = field(default_factory=dict)


def _secret(section: dict[str, Any], name: str, where: str) -> str:
    if name in section:
        raise ConfigError(
            f"{where}.{name}: секрет открытым текстом не принимается — "
            f"задайте {name}_env (переменная окружения) или {name}_file"
        )
    env = section.get(f"{name}_env")
    path = section.get(f"{name}_file")
    if env and path:
        raise ConfigError(f"{where}: задайте что-то одно — {name}_env или {name}_file")
    if env:
        value = os.environ.get(str(env), "")
        if not value:
            raise ConfigError(f"{where}.{name}_env: переменная {env} не задана")
        return value
    if path:
        try:
            value = Path(str(path)).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"{where}.{name}_file: {exc}") from exc
        if not value:
            raise ConfigError(f"{where}.{name}_file: файл {path} пуст")
        return value
    raise ConfigError(f"{where}: нужен {name}_env или {name}_file")


def _str(section: dict[str, Any], name: str, where: str) -> str:
    value = section.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}.{name}: обязательная строка")
    return value.strip()


def _opt_str(section: dict[str, Any], name: str, where: str) -> str | None:
    value = section.get(name)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{where}.{name}: должна быть строкой")
    return value


def _patterns(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where}: нужен список строк")
    return tuple(value)


def parse_unc(path: str) -> tuple[str, str, str]:
    r"""`\\server\share\a\b` (или `//server/share/a/b`) → (server, share, a\b)."""
    norm = path.replace("/", "\\")
    if not norm.startswith("\\\\"):
        raise ConfigError(
            f"путь ресурса должен начинаться с \\\\сервер\\ресурс: {path}"
        )
    parts = [p for p in norm[2:].split("\\") if p]
    if len(parts) < 2:
        raise ConfigError(f"в пути нет имени ресурса: {path}")
    return parts[0], parts[1], "\\".join(parts[2:])


def _kronto(raw: dict[str, Any]) -> KrontoConfig:
    url = _str(raw, "url", "kronto").rstrip("/")
    parts = urlsplit(url)
    if parts.scheme != "https" and parts.hostname not in ("localhost", "127.0.0.1"):
        raise ConfigError("kronto.url: только https")
    key = _secret(raw, "key", "kronto")
    if not key.startswith("kag_"):
        raise ConfigError("kronto.key: ключ агента начинается с kag_")
    return KrontoConfig(url=url, key=key, ca_file=_opt_str(raw, "ca_file", "kronto"))


def _ldap(raw: dict[str, Any]) -> LdapConfig:
    url = _str(raw, "url", "ldap")
    parts = urlsplit(url)
    if parts.scheme not in ("ldap", "ldaps") or not parts.hostname:
        raise ConfigError("ldap.url: ldaps://контроллер или ldap://контроллер")
    start_tls = bool(raw.get("start_tls", False))
    use_ssl = parts.scheme == "ldaps"
    if not use_ssl and not start_tls:
        # Пароль привязки не должен идти по сети открытым текстом.
        raise ConfigError("ldap: без TLS нельзя — ldaps:// или start_tls = true")
    return LdapConfig(
        host=parts.hostname,
        port=parts.port or (636 if use_ssl else 389),
        use_ssl=use_ssl,
        start_tls=start_tls and not use_ssl,
        base_dn=_str(raw, "base_dn", "ldap"),
        bind_dn=_str(raw, "bind_dn", "ldap"),
        password=_secret(raw, "password", "ldap"),
        ca_file=_opt_str(raw, "ca_file", "ldap"),
    )


def _resource(raw: dict[str, Any], i: int) -> ResourceConfig:
    where = f"resources[{i}]"
    server, share, subpath = parse_unc(_str(raw, "path", where))
    port = raw.get("port", 445)
    if not isinstance(port, int):
        raise ConfigError(f"{where}.port: число")
    return ResourceConfig(
        server=server,
        share=share,
        subpath=subpath,
        username=_str(raw, "username", where),
        password=_secret(raw, "password", where),
        port=port,
        encrypt=bool(raw.get("encrypt", False)),
        share_readers=_opt_str(raw, "share_readers", where),
        exclude=_patterns(raw.get("exclude"), f"{where}.exclude"),
    )


def parse_config(raw: dict[str, Any]) -> AgentConfig:
    for name in ("kronto", "ldap"):
        if not isinstance(raw.get(name), dict):
            raise ConfigError(f"нет раздела [{name}]")
    resources_raw = raw.get("resources")
    if not isinstance(resources_raw, list) or not resources_raw:
        raise ConfigError("нужен хотя бы один [[resources]]")
    interval = raw.get("interval_minutes", 60)
    max_mb = raw.get("max_file_mb", 50)
    if not isinstance(interval, int) or interval < 1:
        raise ConfigError("interval_minutes: целое ≥ 1")
    if not isinstance(max_mb, int | float) or max_mb <= 0:
        raise ConfigError("max_file_mb: число > 0")
    local_raw = raw.get("local_groups", {})
    if not isinstance(local_raw, dict):
        raise ConfigError("[local_groups]: таблица SID → список групп")
    local_groups = {
        str(sid).upper(): _patterns(groups, f"local_groups.{sid}")
        for sid, groups in local_raw.items()
    }
    exclude = raw.get("exclude")
    return AgentConfig(
        kronto=_kronto(raw["kronto"]),
        ldap=_ldap(raw["ldap"]),
        resources=tuple(_resource(r, i) for i, r in enumerate(resources_raw)),
        interval_minutes=interval,
        max_file_bytes=int(max_mb * 1024 * 1024),
        exclude=DEFAULT_EXCLUDE if exclude is None else _patterns(exclude, "exclude"),
        upn_fallback=bool(raw.get("upn_fallback", False)),
        local_groups=local_groups,
    )


def load_config(path: str | Path) -> AgentConfig:
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except OSError as exc:
        raise ConfigError(f"не открыть {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return parse_config(raw)
