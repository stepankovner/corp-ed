"""Один обход: state → runs → documents/access → finish (SMB-AGENT.md, §3).

1. Каталог AD (LDAP) — до начала обхода: без него права не вычислить.
2. GET /state — какие файлы и в какой версии уже есть в kronto.
3. POST /runs — начать обход.
4. По каждому ресурсу и файлу: дескриптор безопасности → читатели →
   почты. Новый или изменённый файл (version = `<mtime_ns>-<size>`) —
   PUT /documents с содержимым; прежний — POST /access пачками по 500
   (права могли измениться). Неизвестные kronto из ответа /access — PUT.
5. POST /runs/{id}/finish: complete = true только если ни один ресурс и
   ни одна папка не упали по сети/серверу и обход не прерван. Только
   тогда kronto удаляет то, чего обход не коснулся.

Файл, у которого нет ни одного читателя с почтой (или права не удалось
вычислить), не отправляется; если он уже есть в kronto — права
отзываются через /access с пустым списком.
"""

import logging
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import PurePosixPath
from typing import Any, Protocol

from kronto_agent.config import AgentConfig, ResourceConfig
from kronto_agent.kronto import ACCESS_BATCH, KrontoClient
from kronto_agent.rights import Readers, compute_readers
from kronto_agent.security import SecurityDescriptorError, parse_security_descriptor
from kronto_agent.smb import AccessDeniedError, FileEntry, SmbError, WalkReport

log = logging.getLogger(__name__)


class Resource(Protocol):
    def walk(
        self, extra_exclude: tuple[str, ...], report: WalkReport
    ) -> Iterator[FileEntry]: ...

    def security_descriptor(self, unc: str) -> bytes: ...

    def read(self, unc: str, limit: int) -> bytes: ...


class UserDirectory(Protocol):
    def all_users(self) -> frozenset[str]: ...

    def members(self, sid: str) -> frozenset[str] | None: ...

    def email(self, user_sid: str) -> str | None: ...

    def sid_by_name(self, name: str) -> str | None: ...


ResourceFactory = Callable[[ResourceConfig], AbstractContextManager[Resource]]


@dataclass
class RunReport:
    run_id: str | None = None
    complete: bool = False
    stats: Counter[str] = field(default_factory=Counter)
    # external_id → почты, как отправлены (для журнала и живого теста).
    allowed: dict[str, list[str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass
class _Pending:
    entry: FileEntry
    emails: list[str]


def external_id(entry: FileEntry) -> str:
    return entry.unc.lower()


class Syncer:
    def __init__(
        self,
        cfg: AgentConfig,
        kronto: KrontoClient,
        directory: UserDirectory,
        resource_factory: ResourceFactory,
        stop: threading.Event | None = None,
    ) -> None:
        self.cfg = cfg
        self.kronto = kronto
        self.directory = directory
        self.resource_factory = resource_factory
        self.stop = stop or threading.Event()
        self._readers: dict[bytes, Readers] = {}
        self._no_email: set[str] = set()

    # --- Обход ------------------------------------------------------------

    def run(self) -> RunReport:
        report = RunReport()
        state = self.kronto.state()
        log.info("в kronto уже %d документов", len(state))
        report.run_id = self.kronto.start_run()
        log.info("обход %s начат", report.run_id)
        complete = True
        seen: set[str] = set()
        try:
            for res_cfg in self.cfg.resources:
                if self.stop.is_set():
                    complete = False
                    break
                if not self._resource(res_cfg, state, seen, report):
                    complete = False
            if self.stop.is_set():
                complete = False
                report.errors.append("обход прерван")
        except Exception:
            self._finish_quietly(report, complete=False)
            raise
        report.stats["users_without_email"] = len(self._no_email)
        report.complete = complete and not report.errors
        self.kronto.finish(report.run_id, report.complete, dict(report.stats))
        log.info(
            "обход %s завершён: %s, %s",
            report.run_id,
            "полный" if report.complete else "неполный",
            dict(report.stats),
        )
        return report

    def _finish_quietly(self, report: RunReport, complete: bool) -> None:
        if report.run_id is None:
            return
        try:
            self.kronto.finish(report.run_id, complete, dict(report.stats))
        except Exception as exc:  # закрываем как можем, причина — выше
            log.warning("finish %s: %s", report.run_id, exc)

    def _share_readers(self, res_cfg: ResourceConfig) -> frozenset[str] | None:
        if not res_cfg.share_readers:
            return None
        sid = self.directory.sid_by_name(res_cfg.share_readers)
        members = self.directory.members(sid) if sid else None
        if members is None:
            raise SmbError(
                f"share_readers {res_cfg.share_readers!r} не найдена в каталоге"
            )
        return members

    def _resource(
        self,
        res_cfg: ResourceConfig,
        state: dict[str, str],
        seen: set[str],
        report: RunReport,
    ) -> bool:
        """Обход одного ресурса; False — неполный (ошибки сети/сервера)."""
        stats = report.stats
        walk = WalkReport()
        ok = True
        try:
            share = self._share_readers(res_cfg)
            with self.resource_factory(res_cfg) as resource:
                pending: list[_Pending] = []
                for entry in resource.walk(self.cfg.exclude, walk):
                    if self.stop.is_set():
                        ok = False
                        break
                    ext_id = external_id(entry)
                    if ext_id in seen:
                        continue
                    seen.add(ext_id)
                    stats["files"] += 1
                    if not self._file(resource, entry, share, state, pending, report):
                        ok = False
                    if len(pending) >= ACCESS_BATCH and not self._flush_access(
                        resource, pending, report
                    ):
                        ok = False
                if not self._flush_access(resource, pending, report):
                    ok = False
        except SmbError as exc:
            stats["resources_failed"] += 1
            report.errors.append(str(exc))
            log.error("ресурс %s: %s", res_cfg.unc, exc)
            ok = False
        stats["dirs"] += walk.dirs
        stats["excluded"] += walk.excluded
        stats["reparse_points"] += walk.reparse_points
        stats["dirs_denied"] += walk.dirs_denied
        if walk.errors:
            report.errors.extend(walk.errors)
            ok = False
        return ok

    def _emails(self, readers: frozenset[str]) -> list[str]:
        out = set()
        for sid in readers:
            email = self.directory.email(sid)
            if email:
                out.add(email)
            else:
                self._no_email.add(sid)
        return sorted(out)

    def _file(
        self,
        resource: Resource,
        entry: FileEntry,
        share: frozenset[str] | None,
        state: dict[str, str],
        pending: list[_Pending],
        report: RunReport,
    ) -> bool:
        stats = report.stats
        ext_id = external_id(entry)
        if entry.size > self.cfg.max_file_bytes:
            stats["too_large"] += 1
            return True
        try:
            raw_sd = resource.security_descriptor(entry.unc)
        except AccessDeniedError as exc:
            stats["sd_denied"] += 1
            log.warning("%s", exc)
            return True
        except SmbError as exc:
            report.errors.append(str(exc))
            return False
        readers = self._readers.get(raw_sd)
        if readers is None:
            try:
                readers = compute_readers(
                    parse_security_descriptor(raw_sd), self.directory
                )
            except SecurityDescriptorError as exc:
                log.warning("%s: дескриптор не разобран: %s", entry.unc, exc)
                stats["sd_invalid"] += 1
                readers = Readers(users=frozenset())
            self._readers[raw_sd] = readers
        stats.update(readers.notes)
        users = readers.users & share if share is not None else readers.users
        emails = self._emails(users)
        report.allowed[ext_id] = emails
        if not emails:
            stats["no_readers"] += 1
            if ext_id in state:
                pending.append(_Pending(entry, []))
            return True
        if state.get(ext_id) == entry.version:
            pending.append(_Pending(entry, emails))
            return True
        return self._upload(resource, entry, emails, report)

    def _upload(
        self, resource: Resource, entry: FileEntry, emails: list[str], report: RunReport
    ) -> bool:
        stats = report.stats
        try:
            content = resource.read(entry.unc, self.cfg.max_file_bytes)
        except AccessDeniedError as exc:
            stats["read_denied"] += 1
            log.warning("%s", exc)
            return True
        except ValueError:
            stats["too_large"] += 1
            return True
        except SmbError as exc:
            report.errors.append(str(exc))
            return False
        meta: dict[str, Any] = {
            "run_id": report.run_id,
            "external_id": external_id(entry),
            "version": entry.version,
            "title": PurePosixPath(entry.name).stem or entry.name,
            "path": entry.unc,
            "filename": entry.name,
            "modified_at": entry.modified.astimezone(UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "allowed_emails": emails,
        }
        result = self.kronto.put_document(meta, entry.name, content)
        stats[f"documents_{result.status}"] += 1
        if result.status not in ("added", "updated", "unchanged"):
            log.warning(
                "%s: kronto не принял (%s) %s", entry.unc, result.status, result.detail
            )
        return True

    def _flush_access(
        self, resource: Resource, pending: list[_Pending], report: RunReport
    ) -> bool:
        if not pending:
            return True
        items = list(pending)
        pending.clear()
        docs = [
            {"external_id": external_id(p.entry), "allowed_emails": p.emails}
            for p in items
        ]
        unknown = set(self.kronto.access(report.run_id or "", docs))
        report.stats["access_sent"] += len(docs)
        report.stats["access_revoked"] += sum(1 for p in items if not p.emails)
        ok = True
        for item in items:
            if external_id(item.entry) in unknown and item.emails:
                report.stats["access_unknown"] += 1
                ok = self._upload(resource, item.entry, item.emails, report) and ok
        return ok
