"""Общие подделки для модульных тестов: каталог, ресурс SMB, дескрипторы."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from kronto_agent.config import (
    AgentConfig,
    KrontoConfig,
    LdapConfig,
    ResourceConfig,
)
from kronto_agent.security import (
    AceFlags,
    AceType,
    build_ace,
    build_acl,
    build_security_descriptor,
)
from kronto_agent.smb import (
    AccessDeniedError,
    FileEntry,
    SmbError,
    WalkReport,
    is_excluded,
)

DOM = "S-1-5-21-1000-2000-3000"
ANNA, BORIS, VERA, GLEB, EGOR = (
    f"{DOM}-{rid}" for rid in (1101, 1102, 1103, 1104, 1105)
)
HR = f"{DOM}-1200"
DOMAIN_USERS = f"{DOM}-513"
DOMAIN_ADMINS = f"{DOM}-512"
EVERYONE = "S-1-1-0"
SYSTEM = "S-1-5-18"
FOREIGN = "S-1-5-21-9-9-9-1300"
READ = 0x001200A9  # FILE_GENERIC_READ | FILE_EXECUTE
FULL = 0x001F01FF

EMAILS = {
    ANNA: "anna@example.com",
    BORIS: "boris@example.com",
    VERA: "vera@example.com",
    GLEB: "gleb@example.com",
    EGOR: None,  # без почты
}


@dataclass
class FakeDirectory:
    """Каталог в памяти: группы — множества SID пользователей."""

    groups: dict[str, frozenset[str] | None] = field(
        default_factory=lambda: {HR: frozenset({ANNA, BORIS})}
    )
    emails: dict[str, str | None] = field(default_factory=lambda: dict(EMAILS))
    names: dict[str, str] = field(default_factory=lambda: {"hr": HR})

    def all_users(self) -> frozenset[str]:
        return frozenset(self.emails)

    def members(self, sid: str) -> frozenset[str] | None:
        if sid == DOMAIN_USERS:
            return self.all_users()
        if sid in self.emails:
            return frozenset({sid})
        return self.groups.get(sid)

    def email(self, user_sid: str) -> str | None:
        return self.emails.get(user_sid)

    def sid_by_name(self, name: str) -> str | None:
        return self.names.get(name.lower())


def allow(sid: str, mask: int = READ, flags: int = 0) -> bytes:
    return build_ace(AceType.ACCESS_ALLOWED, flags, mask, sid)


def deny(sid: str, mask: int = READ, flags: int = 0) -> bytes:
    return build_ace(AceType.ACCESS_DENIED, flags, mask, sid)


INH = int(AceFlags.INHERITED)


def sd(*aces: bytes, owner: str = DOMAIN_ADMINS) -> bytes:
    return build_security_descriptor(owner, DOMAIN_USERS, build_acl(list(aces)))


@dataclass
class FakeFile:
    content: bytes
    sd: bytes
    modified: datetime = field(
        default_factory=lambda: datetime(2026, 10, 1, tzinfo=UTC)
    )


@dataclass
class FakeShare:
    """Ресурс SMB в памяти: путь от корня → файл."""

    files: dict[str, FakeFile] = field(default_factory=dict)
    broken_dirs: set[str] = field(default_factory=set)
    denied_dirs: set[str] = field(default_factory=set)
    denied_sd: set[str] = field(default_factory=set)
    broken_sd: set[str] = field(default_factory=set)
    reads: list[str] = field(default_factory=list)


class FakeResource:
    def __init__(self, cfg: ResourceConfig, share: FakeShare) -> None:
        self.cfg = cfg
        self.share = share

    def _rel(self, unc: str) -> str:
        return unc[len(self.cfg.unc) + 1 :]

    def walk(
        self, extra_exclude: tuple[str, ...], report: WalkReport
    ) -> Iterator[FileEntry]:
        patterns = extra_exclude + self.cfg.exclude
        for rel in sorted(self.share.files):
            top = rel.split("\\")[0] if "\\" in rel else ""
            if top in self.share.broken_dirs:
                report.errors.append(f"сбой папки {top}")
                continue
            if top in self.share.denied_dirs:
                report.dirs_denied += 1
                continue
            name = rel.rsplit("\\", 1)[-1]
            if is_excluded(rel, name, patterns):
                report.excluded += 1
                continue
            f = self.share.files[rel]
            yield FileEntry(
                unc=rf"{self.cfg.unc}\{rel}",
                rel=rel,
                name=name,
                size=len(f.content),
                modified=f.modified,
                file_id=hash(rel) & 0xFFFF,
            )

    def security_descriptor(self, unc: str) -> bytes:
        rel = self._rel(unc)
        if rel in self.share.denied_sd:
            raise AccessDeniedError(unc)
        if rel in self.share.broken_sd:
            raise SmbError(unc)
        return self.share.files[rel].sd

    def read(self, unc: str, limit: int) -> bytes:
        rel = self._rel(unc)
        self.share.reads.append(rel)
        return self.share.files[rel].content


def resource_cfg(path: str = r"\\fs01\docs", **kw: object) -> ResourceConfig:
    server, share, sub = path[2:].split("\\", 2) + [""] * (
        3 - len(path[2:].split("\\", 2))
    )
    return ResourceConfig(
        server=server,
        share=share,
        subpath=sub,
        username="CORP\\svc",
        password="x",
        **kw,  # type: ignore[arg-type]
    )


def agent_cfg(*resources: ResourceConfig, **kw: object) -> AgentConfig:
    return AgentConfig(
        kronto=KrontoConfig(url="https://kronto.test", key="kag_tenant_conn_secret"),
        ldap=LdapConfig(
            host="dc1",
            port=636,
            use_ssl=True,
            start_tls=False,
            base_dn="DC=corp",
            bind_dn="svc",
            password="x",
        ),
        resources=resources or (resource_cfg(),),
        **kw,  # type: ignore[arg-type]
    )


def factory(shares: dict[str, FakeShare]):  # type: ignore[no-untyped-def]
    @contextmanager
    def make(cfg: ResourceConfig) -> Iterator[FakeResource]:
        share = shares.get(cfg.unc)
        if share is None:
            raise SmbError(f"{cfg.server}: нет связи")
        yield FakeResource(cfg, share)

    return make
