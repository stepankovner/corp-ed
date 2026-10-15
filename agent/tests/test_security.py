"""Разбор дескриптора безопасности на байтах: ручная сборка и настоящие
дескрипторы с Samba (tests/fixtures, сняты со стенда tests/live/smb)."""

import struct
from pathlib import Path

import pytest

from kronto_agent.security import (
    AceFlags,
    AceType,
    Control,
    SecurityDescriptorError,
    build_ace,
    build_acl,
    build_security_descriptor,
    parse_acl,
    parse_security_descriptor,
    sid_to_bytes,
    sid_to_str,
)
from tests.helpers import DOMAIN_ADMINS, DOMAIN_USERS, HR, READ, allow, deny, sd

FIXTURES = Path(__file__).parent / "fixtures"
STAND = "S-1-5-21-1998244612-1519658268-1362803926"


@pytest.mark.parametrize(
    "sid",
    [
        "S-1-1-0",
        "S-1-5-18",
        "S-1-5-32-544",
        "S-1-5-21-1998244612-1519658268-1362803926-1105",
        "S-1-5",
        "S-1-0x123456789ABC-7",
    ],
)
def test_sid_round_trip(sid: str) -> None:
    raw = sid_to_bytes(sid)
    assert sid_to_str(raw) == (sid, len(raw))


def test_sid_bytes_layout() -> None:
    # S-1-5-32-544: ревизия 1, 2 подчинённых, authority 5 (big-endian),
    # подчинённые — little-endian.
    assert sid_to_bytes("S-1-5-32-544") == bytes.fromhex(
        "01 02 000000000005 20000000 20020000"
    )


@pytest.mark.parametrize(
    "raw",
    [
        b"\x01\x01\x00",
        bytes.fromhex("02 00 000000000001"),
        bytes.fromhex("01 02 000000000005 20000000"),
    ],
)
def test_bad_sid(raw: bytes) -> None:
    with pytest.raises(SecurityDescriptorError):
        sid_to_str(raw)


def test_hand_built_descriptor() -> None:
    data = sd(
        deny(HR, flags=AceFlags.OBJECT_INHERIT | AceFlags.CONTAINER_INHERIT),
        allow(DOMAIN_USERS, flags=AceFlags.INHERITED),
        allow("S-1-3-0", mask=0x10000000, flags=AceFlags.INHERIT_ONLY | 0x3),
    )
    parsed = parse_security_descriptor(data)
    assert parsed.owner == DOMAIN_ADMINS
    assert parsed.group == DOMAIN_USERS
    assert parsed.control & Control.DACL_PRESENT
    assert parsed.dacl is not None
    first, second, third = parsed.dacl
    assert (first.type_, first.sid, first.mask) == (AceType.ACCESS_DENIED, HR, READ)
    assert first.is_deny and not first.inherited and first.reads_data
    assert second.is_allow and second.inherited
    assert third.inherit_only and third.reads_data  # GENERIC_ALL


def test_null_and_empty_dacl() -> None:
    no_dacl = parse_security_descriptor(
        build_security_descriptor(DOMAIN_ADMINS, None, None)
    )
    assert no_dacl.dacl is None
    # DACL_PRESENT, но смещение 0 — тоже NULL DACL.
    raw = bytearray(build_security_descriptor(DOMAIN_ADMINS, None, None))
    struct.pack_into("<H", raw, 2, Control.SELF_RELATIVE | Control.DACL_PRESENT)
    assert parse_security_descriptor(bytes(raw)).dacl is None
    empty = parse_security_descriptor(
        build_security_descriptor(None, None, build_acl([]))
    )
    assert empty.dacl == ()
    assert empty.owner is None


def test_object_and_callback_aces() -> None:
    guid = bytes(range(16))
    sid_b = sid_to_bytes(HR)
    body = struct.pack("<II", READ, 0x3) + guid + guid + sid_b
    obj = struct.pack("<BBH", AceType.ACCESS_DENIED_OBJECT, 0, 4 + len(body)) + body
    app_data = b"artx" + b"\x00" * 4  # условное выражение — не разбираем
    cb_body = struct.pack("<I", READ) + sid_b + app_data
    cb = (
        struct.pack("<BBH", AceType.ACCESS_ALLOWED_CALLBACK, 0, 4 + len(cb_body))
        + cb_body
    )
    aces = parse_acl(build_acl([obj, cb]))
    assert [(a.type_, a.sid) for a in aces] == [
        (AceType.ACCESS_DENIED_OBJECT, HR),
        (AceType.ACCESS_ALLOWED_CALLBACK, HR),
    ]
    assert aces[0].is_deny and aces[0].is_conditional
    assert aces[1].is_allow and aces[1].is_conditional


def test_unknown_ace_type_is_kept() -> None:
    raw = struct.pack("<BBHI", 0x42, 0, 12, READ) + b"\x00" * 4
    (ace,) = parse_acl(build_acl([raw]))
    assert ace.type_ == 0x42 and ace.sid == ""
    assert not (ace.is_allow or ace.is_deny or ace.is_ignored)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b[:10],  # обрезан заголовок
        lambda b: b"\x02" + b[1:],  # ревизия
        lambda b: b[:2] + struct.pack("<H", 0x0004) + b[4:],  # не самоотносительный
        lambda b: b[:4] + struct.pack("<I", 4096) + b[8:],  # смещение владельца
        lambda b: b[:-4],  # ACL обрезан
    ],
)
def test_malformed_descriptor(mutate) -> None:  # type: ignore[no-untyped-def]
    good = sd(allow(DOMAIN_USERS))
    with pytest.raises(SecurityDescriptorError):
        parse_security_descriptor(mutate(good))


def test_ace_size_beyond_acl() -> None:
    ace = bytearray(build_ace(AceType.ACCESS_ALLOWED, 0, READ, HR))
    struct.pack_into("<H", ace, 2, 200)
    with pytest.raises(SecurityDescriptorError):
        parse_acl(build_acl([bytes(ace)]))


def test_samba_descriptor_inherited_deny() -> None:
    """Проекты\\План.txt со стенда: запрет vera и разрешение Domain Users
    унаследованы от папки (ID), AI в Control."""
    parsed = parse_security_descriptor(
        (FIXTURES / "samba-projects-plan.sd").read_bytes()
    )
    assert parsed.control & Control.DACL_AUTO_INHERITED
    assert parsed.owner == "S-1-5-32-544"
    assert parsed.group == f"{STAND}-513"
    assert parsed.dacl is not None
    assert [(a.type_, a.inherited, a.sid, a.mask) for a in parsed.dacl] == [
        (AceType.ACCESS_DENIED, True, f"{STAND}-1105", 0x1200A9),
        (AceType.ACCESS_ALLOWED, True, f"{STAND}-513", 0x1200A9),
        (AceType.ACCESS_ALLOWED, True, "S-1-5-18", 0x1F01FF),
        (AceType.ACCESS_ALLOWED, True, f"{STAND}-512", 0x1F01FF),
        (AceType.ACCESS_ALLOWED, True, f"{STAND}-1112", 0x1200A9),
    ]


def test_samba_descriptor_explicit_allow_first() -> None:
    """Проекты\\Для Веры.txt: явное разрешение vera перед унаследованным
    запретом (канонический порядок, smbcacls)."""
    parsed = parse_security_descriptor(
        (FIXTURES / "samba-projects-for-vera.sd").read_bytes()
    )
    assert parsed.dacl is not None
    head = [(a.type_, a.inherited, a.sid) for a in parsed.dacl[:2]]
    assert head == [
        (AceType.ACCESS_ALLOWED, False, f"{STAND}-1105"),
        (AceType.ACCESS_DENIED, True, f"{STAND}-1105"),
    ]
