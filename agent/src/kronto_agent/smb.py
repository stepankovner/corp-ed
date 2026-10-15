"""Обход ресурса SMB 2/3: каталоги, дескрипторы безопасности, чтение файлов.

Библиотека — smbprotocol (MIT): высокоуровневый smbclient для обхода и
чтения, свой запрос QUERY_INFO для дескриптора безопасности (готовая
функция smbclient не передаёт AdditionalInformation). Вход — Negotiate
(pyspnego): без библиотек Kerberos в образе это NTLM; подпись сообщений
обязательна.

Агенту нужно только чтение: файлы открываются на чтение с разделением
чтения/записи/удаления (не мешает пользователям), дескриптор —
с правом READ_CONTROL.

Ошибки делятся на два вида: «нет доступа/файл исчез» (AccessDeniedError —
агент пропускает объект, полный обход это не портит) и всё остальное
(сеть, сервер): обход помечается неполным, удаления в kronto не будет.
"""

import contextlib
import fnmatch
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from kronto_agent.config import ResourceConfig

log = logging.getLogger(__name__)

FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
# OWNER | GROUP | DACL ([MS-SMB2] 2.2.37, AdditionalInformation).
SD_INFORMATION = 0x1 | 0x2 | 0x4
# NTSTATUS, при которых объект пропускается без порчи обхода.
_SKIP_STATUSES = frozenset(
    {
        0xC0000022,  # STATUS_ACCESS_DENIED
        0xC0000034,  # STATUS_OBJECT_NAME_NOT_FOUND
        0xC000003A,  # STATUS_OBJECT_PATH_NOT_FOUND
        0xC0000043,  # STATUS_SHARING_VIOLATION
        0xC0000056,  # STATUS_DELETE_PENDING
        0xC00000BA,  # STATUS_FILE_IS_A_DIRECTORY
        0xC0000103,  # STATUS_NOT_A_DIRECTORY
    }
)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class AccessDeniedError(Exception):
    """Нет доступа к объекту или он исчез — пропустить."""


class SmbError(Exception):
    """Сбой сети или сервера — обход неполный."""


@dataclass(frozen=True)
class FileEntry:
    unc: str  # \\server\share\папка\файл.docx — как на сервере
    rel: str  # папка\файл.docx — от корня ресурса в настройках
    name: str
    size: int
    modified: datetime
    file_id: int

    @property
    def mtime_ns(self) -> int:
        return (self.modified - _EPOCH) // timedelta(microseconds=1) * 1000

    @property
    def version(self) -> str:
        return f"{self.mtime_ns}-{self.size}"


@dataclass
class WalkReport:
    dirs: int = 0
    excluded: int = 0
    reparse_points: int = 0
    dirs_denied: int = 0
    errors: list[str] = field(default_factory=list)


def is_excluded(rel: str, name: str, patterns: tuple[str, ...]) -> bool:
    r"""Маска без «\» — по имени, с «\» или «/» — по пути от корня ресурса."""
    rel_n = rel.replace("/", "\\").lower()
    name_n = name.lower()
    for pattern in patterns:
        p = pattern.replace("/", "\\").lower()
        target = rel_n if "\\" in p else name_n
        if fnmatch.fnmatchcase(target, p):
            return True
    return False


def _ntstatus(exc: BaseException) -> int | None:
    status = getattr(exc, "ntstatus", None) or getattr(exc, "status", None)
    return int(status) if isinstance(status, int) else None


def _classify(exc: BaseException, what: str) -> Exception:
    status = _ntstatus(exc)
    if status is not None and status in _SKIP_STATUSES:
        return AccessDeniedError(f"{what}: 0x{status:08X}")
    return SmbError(f"{what}: {exc}")


class SmbResource:
    """Один ресурс из настроек: подключение, обход, дескрипторы, чтение."""

    def __init__(self, cfg: ResourceConfig) -> None:
        self.cfg = cfg
        self._cache: dict[str, Any] = {}

    def __enter__(self) -> "SmbResource":
        import smbclient

        try:
            smbclient.register_session(
                self.cfg.server,
                username=self.cfg.username,
                password=self.cfg.password,
                port=self.cfg.port,
                encrypt=self.cfg.encrypt or None,
                connection_timeout=30,
                connection_cache=self._cache,
                auth_protocol="negotiate",
            )
        except Exception as exc:
            raise SmbError(f"{self.cfg.server}: {exc}") from exc
        return self

    def __exit__(self, *_exc: object) -> None:
        from smbclient import reset_connection_cache

        # Закрытие: ошибка уже ничего не меняет.
        with contextlib.suppress(Exception):
            reset_connection_cache(connection_cache=self._cache)

    def _kw(self) -> dict[str, Any]:
        return {"connection_cache": self._cache, "port": self.cfg.port}

    # --- Обход ------------------------------------------------------------

    def walk(
        self, extra_exclude: tuple[str, ...], report: WalkReport
    ) -> Iterator[FileEntry]:
        """Файлы ресурса в глубину; исключения — по маскам, точки повторной
        обработки (ссылки, DFS) не обходятся."""
        import smbclient

        patterns = extra_exclude + self.cfg.exclude
        stack = [""]
        while stack:
            rel_dir = stack.pop()
            unc_dir = rf"{self.cfg.unc}\{rel_dir}" if rel_dir else self.cfg.unc
            try:
                entries = list(smbclient.scandir(unc_dir, **self._kw()))
            except Exception as exc:
                err = _classify(exc, unc_dir)
                if isinstance(err, AccessDeniedError):
                    report.dirs_denied += 1
                    log.warning("нет доступа к папке %s", unc_dir)
                    continue
                report.errors.append(str(err))
                log.error("обход %s: %s", unc_dir, exc)
                continue
            report.dirs += 1
            subdirs = []
            for entry in entries:
                info = entry.smb_info
                name = info.file_name
                rel = rf"{rel_dir}\{name}" if rel_dir else name
                if is_excluded(rel, name, patterns):
                    report.excluded += 1
                    continue
                if info.file_attributes & FILE_ATTRIBUTE_REPARSE_POINT:
                    report.reparse_points += 1
                    continue
                if info.file_attributes & FILE_ATTRIBUTE_DIRECTORY:
                    subdirs.append(rel)
                    continue
                yield FileEntry(
                    unc=rf"{unc_dir}\{name}",
                    rel=rel,
                    name=name,
                    size=int(info.end_of_file),
                    modified=info.last_write_time.astimezone(UTC),
                    file_id=int(info.file_id),
                )
            # Обратный порядок в стеке — папки обходятся по алфавиту.
            stack.extend(sorted(subdirs, key=str.lower, reverse=True))

    # --- Дескриптор безопасности -----------------------------------------

    def security_descriptor(self, unc: str) -> bytes:
        """SECURITY_DESCRIPTOR_RELATIVE файла: owner, group, DACL."""
        from smbclient._io import SMBFileTransaction, SMBRawIO
        from smbprotocol.file_info import InfoType
        from smbprotocol.open import (
            FilePipePrinterAccessMask,
            SMB2QueryInfoRequest,
            SMB2QueryInfoResponse,
        )

        try:
            raw = SMBRawIO(
                unc,
                mode="r",
                share_access="rwd",
                desired_access=FilePipePrinterAccessMask.READ_CONTROL
                | FilePipePrinterAccessMask.FILE_READ_ATTRIBUTES,
                **self._kw(),
            )
            req = SMB2QueryInfoRequest()
            req["info_type"] = InfoType.SMB2_0_INFO_SECURITY
            req["output_buffer_length"] = 65536
            req["additional_information"] = SD_INFORMATION
            req["file_id"] = raw.fd.file_id

            def receive(request: Any) -> bytes:
                response = raw.fd.connection.receive(request)
                resp = SMB2QueryInfoResponse()
                resp.unpack(response["data"].get_value())
                return bytes(resp["buffer"].get_value())

            with SMBFileTransaction(raw) as transaction:
                transaction += (req, receive)
            return bytes(transaction.results[0])
        except Exception as exc:
            raise _classify(exc, f"дескриптор {unc}") from exc

    # --- Чтение ------------------------------------------------------------

    def read(self, unc: str, limit: int) -> bytes:
        """Содержимое файла; файл вырос после листинга больше limit —
        ValueError (пропустить, как слишком большой)."""
        import smbclient

        try:
            with smbclient.open_file(
                unc, mode="rb", share_access="rwd", **self._kw()
            ) as fh:
                data = fh.read(limit + 1)
        except Exception as exc:
            raise _classify(exc, f"чтение {unc}") from exc
        if len(data) > limit:
            raise ValueError(f"{unc}: больше {limit} байт")
        return bytes(data)
