"""Вычисление читателей по DACL: группы, запреты, наследование, порядок."""

from pathlib import Path

import pytest

from kronto_agent.rights import canonical_order, compute_readers
from kronto_agent.security import (
    GENERIC_ALL,
    GENERIC_READ,
    READ_CONTROL,
    AceFlags,
    AceType,
    build_ace,
    build_security_descriptor,
    parse_security_descriptor,
)
from tests.helpers import (
    ANNA,
    BORIS,
    DOMAIN_USERS,
    EGOR,
    EVERYONE,
    FOREIGN,
    GLEB,
    HR,
    INH,
    SYSTEM,
    VERA,
    FakeDirectory,
    allow,
    deny,
    sd,
)

ALL = {ANNA, BORIS, VERA, GLEB, EGOR}


def readers(raw: bytes, directory: FakeDirectory | None = None) -> set[str]:
    return set(
        compute_readers(
            parse_security_descriptor(raw), directory or FakeDirectory()
        ).users
    )


def test_group_and_wide_sids() -> None:
    assert readers(sd(allow(HR))) == {ANNA, BORIS}
    assert readers(sd(allow(DOMAIN_USERS))) == ALL
    assert readers(sd(allow(EVERYONE))) == ALL
    assert readers(sd(allow("S-1-5-11"))) == ALL  # Authenticated Users
    assert readers(sd(allow(SYSTEM), allow("S-1-3-0"))) == set()


def test_explicit_deny_beats_allow() -> None:
    assert readers(sd(deny(VERA), allow(DOMAIN_USERS))) == ALL - {VERA}
    # Запрет группе снимает и тех, кому разрешено персонально ниже.
    assert readers(sd(deny(HR), allow(ANNA), allow(GLEB))) == {GLEB}


def test_explicit_allow_beats_inherited_deny() -> None:
    """Как «Проекты\\Для Веры.txt» на стенде: явное разрешение раньше
    унаследованного запрета."""
    raw = sd(allow(VERA), deny(VERA, flags=INH), allow(DOMAIN_USERS, flags=INH))
    assert readers(raw) == ALL


def test_inherited_deny_and_allow() -> None:
    raw = sd(deny(VERA, flags=INH), allow(DOMAIN_USERS, flags=INH))
    assert readers(raw) == ALL - {VERA}


def test_non_canonical_takes_stricter_result() -> None:
    # Явное разрешение раньше явного запрета: Windows выдал бы vera доступ
    # (порядок хранения), канонический порядок — нет. Берём строже.
    result = compute_readers(
        parse_security_descriptor(sd(allow(VERA), deny(VERA))), FakeDirectory()
    )
    assert result.users == frozenset()
    assert result.notes["non_canonical"] == 1
    # Унаследованное разрешение раньше явного запрета — тоже строже.
    assert readers(sd(allow(DOMAIN_USERS, flags=INH), deny(GLEB))) == ALL - {GLEB}


def test_canonical_order() -> None:
    aces = parse_security_descriptor(
        sd(allow(ANNA, flags=INH), allow(BORIS), deny(VERA), deny(GLEB, flags=INH))
    ).dacl
    assert aces is not None
    assert [a.sid for a in canonical_order(aces)] == [VERA, BORIS, ANNA, GLEB]


def test_inherit_only_does_not_apply() -> None:
    io = AceFlags.INHERIT_ONLY | AceFlags.OBJECT_INHERIT
    assert readers(sd(allow(EVERYONE, flags=io), allow(ANNA))) == {ANNA}
    assert readers(sd(deny(ANNA, flags=io), allow(ANNA))) == {ANNA}


@pytest.mark.parametrize(
    ("mask", "reads"),
    [
        (0x1, True),  # FILE_READ_DATA
        (GENERIC_READ, True),
        (GENERIC_ALL, True),
        (0x001F01FF, True),  # FILE_ALL_ACCESS
        (READ_CONTROL | 0x80, False),  # права и атрибуты — не содержимое
        (0x2 | 0x4, False),  # запись без чтения
        (0x20, False),  # FILE_EXECUTE
    ],
)
def test_read_mask(mask: int, reads: bool) -> None:
    assert readers(sd(allow(ANNA, mask=mask))) == ({ANNA} if reads else set())
    denied = readers(sd(deny(ANNA, mask=mask), allow(ANNA)))
    assert denied == (set() if reads else {ANNA})


def test_unresolved_sids() -> None:
    directory = FakeDirectory(groups={HR: frozenset({ANNA, BORIS}), FOREIGN: None})
    result = compute_readers(
        parse_security_descriptor(sd(allow(FOREIGN), allow(HR))), directory
    )
    assert result.users == {ANNA, BORIS}
    assert result.notes["unresolved_allow"] == 1
    # Запрет по неизвестному SID — запрет всем, кому чтение ещё не выдано.
    result = compute_readers(
        parse_security_descriptor(
            sd(allow(GLEB), deny(FOREIGN, flags=INH), allow(HR, flags=INH))
        ),
        directory,
    )
    assert result.users == {GLEB}
    assert result.notes["unresolved_deny"] == 1
    # BUILTIN без сопоставления (каталог вернул None) — так же.
    assert readers(sd(allow("S-1-5-32-545")), directory) == set()


def test_conditional_aces() -> None:
    cb_allow = build_ace(AceType.ACCESS_ALLOWED_CALLBACK, 0, 0x1, ANNA)
    cb_deny = build_ace(AceType.ACCESS_DENIED_CALLBACK, 0, 0x1, BORIS)
    result = compute_readers(
        parse_security_descriptor(sd(cb_deny, cb_allow, allow(HR))), FakeDirectory()
    )
    # Условное разрешение не засчитано (anna — через hr), условный запрет
    # применён безусловно.
    assert result.users == {ANNA}
    assert result.notes["conditional_allow"] == 1


def test_unknown_ace_type_hides_file() -> None:
    import struct

    unknown = struct.pack("<BBHI", 0x42, 0, 12, 0x1) + b"\x00" * 4
    result = compute_readers(
        parse_security_descriptor(sd(allow(EVERYONE), unknown)), FakeDirectory()
    )
    assert result.users == frozenset()
    assert result.notes["unknown_ace"] == 1


def test_audit_ace_ignored() -> None:
    audit = build_ace(AceType.SYSTEM_AUDIT, 0x40, 0x1, EVERYONE)
    assert readers(sd(audit, allow(ANNA))) == {ANNA}


def test_null_and_empty_dacl() -> None:
    null = build_security_descriptor(ANNA, None, None)
    assert readers(null) == ALL
    assert readers(sd()) == set()


def test_owner_rights() -> None:
    assert readers(sd(allow("S-1-3-4"), owner=BORIS)) == {BORIS}
    assert readers(sd(deny("S-1-3-4"), allow(HR), owner=BORIS)) == {ANNA}


FIXTURES = Path(__file__).parent / "fixtures"
STAND = "S-1-5-21-1998244612-1519658268-1362803926"


def test_samba_fixtures() -> None:
    """Настоящие дескрипторы со стенда, каталог стенда: vera — 1105."""
    vera = f"{STAND}-1105"
    users = {
        f"{STAND}-{rid}": f"u{rid}@example.com" for rid in (1103, 1104, 1105, 1106)
    }
    users[vera] = "vera@example.com"

    class StandDirectory(FakeDirectory):
        def members(self, sid: str) -> frozenset[str] | None:
            if sid == f"{STAND}-513":
                return self.all_users()
            if sid in self.emails:
                return frozenset({sid})
            # Domain Admins, группа агента — без пользователей с почтой.
            return frozenset() if sid.startswith(STAND) else None

    directory = StandDirectory(groups={}, emails=dict(users))
    plan = (FIXTURES / "samba-projects-plan.sd").read_bytes()
    assert readers(plan, directory) == set(users) - {vera}
    for_vera = (FIXTURES / "samba-projects-for-vera.sd").read_bytes()
    assert readers(for_vera, directory) == set(users)
