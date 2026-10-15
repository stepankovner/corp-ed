"""Разбор дескриптора безопасности Windows (самоотносительный формат).

Сервер SMB на QUERY_INFO (SMB2_0_INFO_SECURITY) отдаёт
SECURITY_DESCRIPTOR в самоотносительном формате: заголовок со смещениями
на SID владельца, SID группы, SACL и DACL. Разбираем сами — в smbprotocol
есть только структуры для создания дескриптора, без ACE с флагами
наследования и типов callback/object.

Форматы — [MS-DTYP]:
- 2.4.6 SECURITY_DESCRIPTOR
  https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dtyp/7d4dac05-9cef-4563-a058-f108abecce1d
- 2.4.5 ACL
  https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dtyp/20233ed8-a6c6-4097-aafa-dd545ed24428
- 2.4.4.1 ACE_HEADER
  https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dtyp/628ebb1d-c509-4ea0-a10f-77ef97ca4586
- 2.4.2.2 SID
  https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dtyp/f992ad60-0fe4-4b87-9fed-beb478836861

Любая несостыковка длин и смещений — SecurityDescriptorError: права файла,
который не удалось разобрать, не вычисляются (файл никому не виден).
"""

import struct
from dataclasses import dataclass
from enum import IntEnum, IntFlag


class SecurityDescriptorError(ValueError):
    """Дескриптор безопасности не разбирается."""


class Control(IntFlag):
    """Флаги SECURITY_DESCRIPTOR.Control (нужные агенту)."""

    OWNER_DEFAULTED = 0x0001
    GROUP_DEFAULTED = 0x0002
    DACL_PRESENT = 0x0004
    DACL_DEFAULTED = 0x0008
    SACL_PRESENT = 0x0010
    DACL_AUTO_INHERITED = 0x0400
    SACL_AUTO_INHERITED = 0x0800
    DACL_PROTECTED = 0x1000
    SACL_PROTECTED = 0x2000
    SELF_RELATIVE = 0x8000


class AceType(IntEnum):
    ACCESS_ALLOWED = 0x00
    ACCESS_DENIED = 0x01
    SYSTEM_AUDIT = 0x02
    SYSTEM_ALARM = 0x03
    ACCESS_ALLOWED_COMPOUND = 0x04
    ACCESS_ALLOWED_OBJECT = 0x05
    ACCESS_DENIED_OBJECT = 0x06
    SYSTEM_AUDIT_OBJECT = 0x07
    SYSTEM_ALARM_OBJECT = 0x08
    ACCESS_ALLOWED_CALLBACK = 0x09
    ACCESS_DENIED_CALLBACK = 0x0A
    ACCESS_ALLOWED_CALLBACK_OBJECT = 0x0B
    ACCESS_DENIED_CALLBACK_OBJECT = 0x0C
    SYSTEM_AUDIT_CALLBACK = 0x0D
    SYSTEM_ALARM_CALLBACK = 0x0E
    SYSTEM_AUDIT_CALLBACK_OBJECT = 0x0F
    SYSTEM_ALARM_CALLBACK_OBJECT = 0x10
    SYSTEM_MANDATORY_LABEL = 0x11
    SYSTEM_RESOURCE_ATTRIBUTE = 0x12
    SYSTEM_SCOPED_POLICY_ID = 0x13


class AceFlags(IntFlag):
    OBJECT_INHERIT = 0x01
    CONTAINER_INHERIT = 0x02
    NO_PROPAGATE_INHERIT = 0x04
    INHERIT_ONLY = 0x08
    INHERITED = 0x10
    SUCCESSFUL_ACCESS = 0x40
    FAILED_ACCESS = 0x80


# Биты маски доступа ([MS-DTYP] 2.4.3, [MS-SMB2] 2.2.13.1.1).
FILE_READ_DATA = 0x00000001
READ_CONTROL = 0x00020000
GENERIC_ALL = 0x10000000
GENERIC_EXECUTE = 0x20000000
GENERIC_WRITE = 0x40000000
GENERIC_READ = 0x80000000
# Чтение содержимого: FILE_READ_DATA или общие права, которые его включают
# (GENERIC_READ → FILE_GENERIC_READ, GENERIC_ALL → FILE_ALL_ACCESS).
READ_DATA_BITS = FILE_READ_DATA | GENERIC_READ | GENERIC_ALL

ALLOW_TYPES = frozenset(
    {
        AceType.ACCESS_ALLOWED,
        AceType.ACCESS_ALLOWED_OBJECT,
        AceType.ACCESS_ALLOWED_CALLBACK,
        AceType.ACCESS_ALLOWED_CALLBACK_OBJECT,
    }
)
DENY_TYPES = frozenset(
    {
        AceType.ACCESS_DENIED,
        AceType.ACCESS_DENIED_OBJECT,
        AceType.ACCESS_DENIED_CALLBACK,
        AceType.ACCESS_DENIED_CALLBACK_OBJECT,
    }
)
# Условные (callback) и объектные ACE: у файлов их почти не бывает;
# разрешение по ним агент не засчитывает, запрет — засчитывает безусловно.
CONDITIONAL_TYPES = frozenset(
    {
        AceType.ACCESS_ALLOWED_OBJECT,
        AceType.ACCESS_DENIED_OBJECT,
        AceType.ACCESS_ALLOWED_CALLBACK,
        AceType.ACCESS_DENIED_CALLBACK,
        AceType.ACCESS_ALLOWED_CALLBACK_OBJECT,
        AceType.ACCESS_DENIED_CALLBACK_OBJECT,
    }
)
# Не влияют на доступ (аудит, метки целостности, атрибуты ресурса).
IGNORED_TYPES = frozenset(
    {
        AceType.SYSTEM_AUDIT,
        AceType.SYSTEM_ALARM,
        AceType.SYSTEM_AUDIT_OBJECT,
        AceType.SYSTEM_ALARM_OBJECT,
        AceType.SYSTEM_AUDIT_CALLBACK,
        AceType.SYSTEM_ALARM_CALLBACK,
        AceType.SYSTEM_AUDIT_CALLBACK_OBJECT,
        AceType.SYSTEM_ALARM_CALLBACK_OBJECT,
        AceType.SYSTEM_MANDATORY_LABEL,
        AceType.SYSTEM_RESOURCE_ATTRIBUTE,
        AceType.SYSTEM_SCOPED_POLICY_ID,
    }
)

_OBJECT_TYPES = frozenset(
    {
        AceType.ACCESS_ALLOWED_OBJECT,
        AceType.ACCESS_DENIED_OBJECT,
        AceType.SYSTEM_AUDIT_OBJECT,
        AceType.SYSTEM_ALARM_OBJECT,
        AceType.ACCESS_ALLOWED_CALLBACK_OBJECT,
        AceType.ACCESS_DENIED_CALLBACK_OBJECT,
        AceType.SYSTEM_AUDIT_CALLBACK_OBJECT,
        AceType.SYSTEM_ALARM_CALLBACK_OBJECT,
    }
)
# ACE_OBJECT_TYPE_PRESENT / ACE_INHERITED_OBJECT_TYPE_PRESENT ([MS-DTYP] 2.4.4.3).
_OBJECT_TYPE_PRESENT = 0x1
_INHERITED_OBJECT_TYPE_PRESENT = 0x2


@dataclass(frozen=True)
class Ace:
    """Одна запись ACL. type_ — число: неизвестный тип тоже сохраняется."""

    type_: int
    flags: int
    mask: int
    sid: str

    @property
    def inherited(self) -> bool:
        return bool(self.flags & AceFlags.INHERITED)

    @property
    def inherit_only(self) -> bool:
        return bool(self.flags & AceFlags.INHERIT_ONLY)

    @property
    def is_allow(self) -> bool:
        return self.type_ in ALLOW_TYPES

    @property
    def is_deny(self) -> bool:
        return self.type_ in DENY_TYPES

    @property
    def is_conditional(self) -> bool:
        return self.type_ in CONDITIONAL_TYPES

    @property
    def is_ignored(self) -> bool:
        return self.type_ in IGNORED_TYPES

    @property
    def reads_data(self) -> bool:
        return bool(self.mask & READ_DATA_BITS)


@dataclass(frozen=True)
class SecurityDescriptor:
    control: int
    owner: str | None
    group: str | None
    # None — DACL нет (NULL DACL: доступ всем); пустой кортеж — никому.
    dacl: tuple[Ace, ...] | None

    @property
    def protected(self) -> bool:
        return bool(self.control & Control.DACL_PROTECTED)


def sid_to_str(data: bytes, offset: int = 0) -> tuple[str, int]:
    """SID из байтов → ("S-1-5-21-…", длина в байтах)."""
    if len(data) < offset + 8:
        raise SecurityDescriptorError("SID обрезан")
    revision, count = data[offset], data[offset + 1]
    if revision != 1 or count > 15:
        raise SecurityDescriptorError(f"неверный SID: ревизия {revision}, {count}")
    size = 8 + 4 * count
    if len(data) < offset + size:
        raise SecurityDescriptorError("SID обрезан")
    authority = int.from_bytes(data[offset + 2 : offset + 8], "big")
    subs = struct.unpack_from(f"<{count}I", data, offset + 8)
    auth = str(authority) if authority < 2**32 else f"0x{authority:012X}"
    return "-".join(["S", "1", auth, *map(str, subs)]), size


def sid_to_bytes(sid: str) -> bytes:
    """Строка SID → байты (для поиска objectSid в LDAP и для тестов)."""
    parts = sid.split("-")
    if len(parts) < 3 or parts[0].upper() != "S" or parts[1] != "1":
        raise ValueError(f"неверный SID: {sid}")
    auth_s = parts[2]
    authority = int(auth_s, 16) if auth_s.lower().startswith("0x") else int(auth_s)
    subs = [int(p) for p in parts[3:]]
    if len(subs) > 15:
        raise ValueError(f"неверный SID: {sid}")
    return (
        bytes([1, len(subs)])
        + authority.to_bytes(6, "big")
        + struct.pack(f"<{len(subs)}I", *subs)
    )


def _parse_ace(data: bytes, offset: int) -> tuple[Ace, int]:
    if len(data) < offset + 4:
        raise SecurityDescriptorError("ACE обрезан")
    ace_type, flags, size = struct.unpack_from("<BBH", data, offset)
    if size < 8 or size % 4 or len(data) < offset + size:
        raise SecurityDescriptorError(f"неверный размер ACE: {size}")
    body = data[offset : offset + size]
    mask = struct.unpack_from("<I", body, 4)[0]
    pos = 8
    if ace_type in _OBJECT_TYPES:
        if size < 12:
            raise SecurityDescriptorError("объектный ACE обрезан")
        obj_flags = struct.unpack_from("<I", body, 8)[0]
        pos = 12
        if obj_flags & _OBJECT_TYPE_PRESENT:
            pos += 16
        if obj_flags & _INHERITED_OBJECT_TYPE_PRESENT:
            pos += 16
    known = ace_type in AceType._value2member_map_
    if known and ace_type != AceType.ACCESS_ALLOWED_COMPOUND:
        sid, _ = sid_to_str(body, pos)
    else:
        # Неизвестный (или составной) тип: SID не ищем, тип сохраняем —
        # вычисление прав такой ACL не примет.
        sid = ""
    return Ace(type_=ace_type, flags=flags, mask=mask, sid=sid), size


def parse_acl(data: bytes, offset: int = 0) -> tuple[Ace, ...]:
    if len(data) < offset + 8:
        raise SecurityDescriptorError("ACL обрезан")
    revision, _sbz1, acl_size, count, _sbz2 = struct.unpack_from("<BBHHH", data, offset)
    if revision not in (2, 4):
        raise SecurityDescriptorError(f"неверная ревизия ACL: {revision}")
    if acl_size < 8 or len(data) < offset + acl_size:
        raise SecurityDescriptorError("ACL обрезан")
    acl = data[offset : offset + acl_size]
    aces: list[Ace] = []
    pos = 8
    for _ in range(count):
        ace, size = _parse_ace(acl, pos)
        aces.append(ace)
        pos += size
    return tuple(aces)


def parse_security_descriptor(data: bytes) -> SecurityDescriptor:
    """SECURITY_DESCRIPTOR_RELATIVE → SecurityDescriptor."""
    if len(data) < 20:
        raise SecurityDescriptorError("дескриптор обрезан")
    revision, _sbz1, control, off_owner, off_group, _off_sacl, off_dacl = (
        struct.unpack_from("<BBHIIII", data, 0)
    )
    if revision != 1:
        raise SecurityDescriptorError(f"неверная ревизия дескриптора: {revision}")
    if not control & Control.SELF_RELATIVE:
        raise SecurityDescriptorError("дескриптор не в самоотносительном формате")
    for off in (off_owner, off_group, off_dacl):
        if off and (off < 20 or off >= len(data)):
            raise SecurityDescriptorError(f"смещение вне дескриптора: {off}")
    owner = sid_to_str(data, off_owner)[0] if off_owner else None
    group = sid_to_str(data, off_group)[0] if off_group else None
    dacl: tuple[Ace, ...] | None = None
    if control & Control.DACL_PRESENT and off_dacl:
        dacl = parse_acl(data, off_dacl)
    # DACL_PRESENT со смещением 0 — тоже NULL DACL ([MS-DTYP] 2.4.6).
    return SecurityDescriptor(control=control, owner=owner, group=group, dacl=dacl)


# --- Сборка байтов (для тестов и фикстур) --------------------------------


def build_ace(type_: int, flags: int, mask: int, sid: str) -> bytes:
    """Простой ACE (ALLOWED/DENIED/CALLBACK без данных приложения)."""
    sid_b = sid_to_bytes(sid)
    size = 8 + len(sid_b)
    return struct.pack("<BBHI", type_, flags, size, mask) + sid_b


def build_acl(aces: list[bytes], revision: int = 2) -> bytes:
    body = b"".join(aces)
    return struct.pack("<BBHHH", revision, 0, 8 + len(body), len(aces), 0) + body


def build_security_descriptor(
    owner: str | None,
    group: str | None,
    dacl: bytes | None,
    control: int = 0,
) -> bytes:
    """Самоотносительный дескриптор: заголовок, owner, group, DACL."""
    parts = bytearray(20)
    offsets = [0, 0, 0, 0]
    for i, chunk in (
        (0, sid_to_bytes(owner) if owner else None),
        (1, sid_to_bytes(group) if group else None),
        (3, dacl),
    ):
        if chunk is None:
            continue
        offsets[i] = len(parts)
        parts += chunk
    ctl = control | Control.SELF_RELATIVE
    if dacl is not None:
        ctl |= Control.DACL_PRESENT
    struct.pack_into("<BBHIIII", parts, 0, 1, 0, ctl, *offsets)
    return bytes(parts)
