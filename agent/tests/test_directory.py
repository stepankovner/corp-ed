"""Каталог AD поверх поддельного LDAP: транзитивные группы, основная
группа, широкие SID, отключённые учётки, почта/UPN, BUILTIN, кэш."""

import re
from dataclasses import dataclass, field

import pytest

from kronto_agent.directory import (
    AdDirectory,
    DirectoryError,
    Entry,
    escape_filter_value,
)
from kronto_agent.security import sid_to_bytes

BASE = "DC=corp,DC=example"
DOM = "S-1-5-21-11-22-33"


def _unescape(value: str) -> bytes:
    out = bytearray()
    i = 0
    while i < len(value):
        if value[i] == "\\":
            out.append(int(value[i + 1 : i + 3], 16))
            i += 3
        else:
            out += value[i].encode()
            i += 1
    return bytes(out)


@dataclass
class Obj:
    dn: str
    classes: list[str]
    sid: str | None = None
    mail: str | None = None
    upn: str | None = None
    uac: int = 512
    primary: int | None = None
    sam: str | None = None
    members: list[str] = field(default_factory=list)

    def attrs(self) -> dict[str, list[bytes]]:
        a: dict[str, list[bytes]] = {"objectclass": [c.encode() for c in self.classes]}
        if self.sid:
            a["objectsid"] = [sid_to_bytes(self.sid)]
        if self.mail:
            a["mail"] = [self.mail.encode()]
        if self.upn:
            a["userprincipalname"] = [self.upn.encode()]
        if self.primary is not None:
            a["primarygroupid"] = [str(self.primary).encode()]
        cn = self.dn.split(",")[0].split("=", 1)[1]
        a["cn"] = [cn.encode()]
        return a


class FakeLdap:
    """Понимает ровно те фильтры, что строит AdDirectory."""

    def __init__(self, objects: list[Obj]) -> None:
        self.objects = {o.dn.lower(): o for o in objects}
        self.searches: list[str] = []

    def _closure(self, dn: str) -> set[str]:
        seen: set[str] = set()
        stack = list(self.objects[dn.lower()].members)
        while stack:
            m = stack.pop().lower()
            if m in seen:
                continue
            seen.add(m)
            stack.extend(self.objects[m].members)
        return seen

    def __call__(
        self, base: str, scope: str, flt: str, attributes: list[str]
    ) -> list[Entry]:
        self.searches.append(flt)
        objs = list(self.objects.values())
        if scope == "base":
            found = [self.objects[base.lower()]]
        elif "objectCategory=person" in flt:
            found = [
                o
                for o in objs
                if "user" in o.classes and "computer" not in o.classes and not o.uac & 2
            ]
        elif m := re.fullmatch(r"\(objectSid=(.*)\)", flt):
            raw = _unescape(m.group(1))
            found = [o for o in objs if o.sid and sid_to_bytes(o.sid) == raw]
        elif m := re.fullmatch(
            r"\(memberOf:1\.2\.840\.113556\.1\.4\.1941:=(.*)\)", flt
        ):
            dn = _unescape(m.group(1)).decode()
            found = [self.objects[d] for d in sorted(self._closure(dn))]
        elif m := re.fullmatch(r"\(sAMAccountName=(.*)\)", flt):
            name = _unescape(m.group(1)).decode().lower()
            found = [o for o in objs if (o.sam or "").lower() == name]
        else:
            raise AssertionError(f"неожиданный фильтр {flt}")
        return [(o.dn, o.attrs()) for o in found]


def dn(cn: str, container: str = "CN=Users") -> str:
    return f"CN={cn},{container},{BASE}"


def user(
    cn: str, rid: int, mail: str | None = None, uac: int = 512, primary: int = 513
) -> Obj:
    return Obj(
        dn(cn),
        ["top", "person", "user"],
        f"{DOM}-{rid}",
        mail=mail,
        upn=f"{cn}@corp.example",
        uac=uac,
        primary=primary,
        sam=cn,
    )


def group(cn: str, sid: str, members: list[str], container: str = "CN=Users") -> Obj:
    return Obj(dn(cn, container), ["top", "group"], sid, sam=cn, members=members)


def make_ldap() -> FakeLdap:
    return FakeLdap(
        [
            Obj(BASE, ["domain"], DOM),
            user("anna", 1101, "Anna@Example.com"),
            user("boris", 1102, "boris@example.com"),
            user("vera", 1103, "vera@example.com"),
            user("dmitry", 1104, "dmitry@example.com", uac=514),
            user("egor", 1105),
            user("kira", 1106, "kira@example.com", primary=1300),
            Obj(
                dn("pc1", "CN=Computers"),
                ["user", "computer"],
                f"{DOM}-1150",
                primary=515,
            ),
            group("hr-leads", f"{DOM}-1201", [dn("boris")]),
            group("hr", f"{DOM}-1200", [dn("anna"), dn("dmitry"), dn("hr-leads")]),
            group("staff", f"{DOM}-1300", [dn("vera")]),
            group("Domain Users", f"{DOM}-513", []),
            group("all-wrapper", f"{DOM}-1400", [dn("Domain Users")]),
            group(
                "auth-wrapper",
                f"{DOM}-1401",
                [dn("S-1-5-11", "CN=ForeignSecurityPrincipals")],
            ),
            group(
                "partner",
                f"{DOM}-1402",
                [dn("S-1-5-21-9-9-9-500", "CN=ForeignSecurityPrincipals")],
            ),
            Obj(
                dn("S-1-5-11", "CN=ForeignSecurityPrincipals"),
                ["foreignSecurityPrincipal"],
                "S-1-5-11",
            ),
            Obj(
                dn("S-1-5-21-9-9-9-500", "CN=ForeignSecurityPrincipals"),
                ["foreignSecurityPrincipal"],
                "S-1-5-21-9-9-9-500",
            ),
            group("Users", "S-1-5-32-545", [dn("Domain Users")], "CN=Builtin"),
        ]
    )


ANNA, BORIS, VERA, DMITRY, EGOR, KIRA = (f"{DOM}-{r}" for r in range(1101, 1107))
ENABLED = frozenset({ANNA, BORIS, VERA, EGOR, KIRA})


def test_users_and_emails() -> None:
    d = AdDirectory(make_ldap(), BASE)
    assert d.domain_sid == DOM
    assert set(d.users) == ENABLED  # без отключённого dmitry и компьютера
    assert d.email(ANNA) == "anna@example.com"  # в нижнем регистре
    assert d.email(EGOR) is None
    assert (
        AdDirectory(make_ldap(), BASE, upn_fallback=True).email(EGOR)
        == "egor@corp.example"
    )


def test_transitive_group_without_disabled() -> None:
    d = AdDirectory(make_ldap(), BASE)
    assert d.members(f"{DOM}-1200") == frozenset({ANNA, BORIS})


def test_primary_group_members() -> None:
    d = AdDirectory(make_ldap(), BASE)
    assert d.members(f"{DOM}-1300") == frozenset({VERA, KIRA})


@pytest.mark.parametrize(
    "sid",
    ["S-1-1-0", "S-1-5-11", "S-1-5-2", f"{DOM}-513", f"{DOM}-1400", f"{DOM}-1401"],
)
def test_wide_sids(sid: str) -> None:
    assert AdDirectory(make_ldap(), BASE).members(sid) == ENABLED


def test_single_users() -> None:
    d = AdDirectory(make_ldap(), BASE)
    assert d.members(VERA) == frozenset({VERA})
    assert d.members(DMITRY) == frozenset()  # отключён
    assert d.members(f"{DOM}-1150") == frozenset()  # компьютер


def test_unresolvable() -> None:
    d = AdDirectory(make_ldap(), BASE)
    assert d.members("S-1-5-21-77-77-77-1000") is None  # нет в каталоге
    assert d.members("S-1-5-21-9-9-9-500") is None  # внешний участник
    assert d.members("S-1-5-32-545") is None  # BUILTIN без сопоставления
    # Группа с внешним участником: свои — да, внешний — не раскрывается.
    assert d.members(f"{DOM}-1402") == frozenset()


def test_local_groups_mapping() -> None:
    d = AdDirectory(
        make_ldap(),
        BASE,
        local_groups={
            "S-1-5-32-545": ("Domain Users",),
            "S-1-5-32-555": ("KRONTO\\hr", f"{DOM}-1300"),
            "S-1-5-32-556": ("no-such-group",),
        },
    )
    assert d.members("S-1-5-32-545") == ENABLED
    assert d.members("S-1-5-32-555") == frozenset({ANNA, BORIS, VERA, KIRA})
    assert d.members("S-1-5-32-556") is None


def test_cache_per_directory() -> None:
    ldap = make_ldap()
    d = AdDirectory(ldap, BASE)
    d.members(f"{DOM}-1200")
    n = len(ldap.searches)
    assert d.members(f"{DOM}-1200") == frozenset({ANNA, BORIS})
    assert len(ldap.searches) == n


def test_sid_by_name() -> None:
    d = AdDirectory(make_ldap(), BASE)
    assert d.sid_by_name("CORP\\hr") == f"{DOM}-1200"
    assert d.sid_by_name("nobody") is None
    assert d.sid_by_name("s-1-5-32-545") == "S-1-5-32-545"


def test_base_without_sid() -> None:
    ldap = FakeLdap([Obj(BASE, ["container"])])
    with pytest.raises(DirectoryError):
        AdDirectory(ldap, BASE)


def test_escape_filter_value() -> None:
    assert escape_filter_value("CN=a*(b)\\c,DC=x") == "CN=a\\2a\\28b\\29\\5cc,DC=x"
    assert escape_filter_value(b"\x01\x05\x00") == "\\01\\05\\00"
