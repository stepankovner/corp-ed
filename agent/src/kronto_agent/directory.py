"""Каталог AD по LDAP: SID → включённые пользователи → почты.

Библиотека — ldap3 (LGPL-3.0, почему допустимо — README.md). Всё, что
агент спрашивает у каталога, — четыре вида запросов (см. LdapSearch):

- все включённые пользователи домена (один раз за обход): objectSid,
  mail, userPrincipalName, primaryGroupID;
- объект по objectSid (двоичный, экранированный в фильтре);
- транзитивные участники группы: `memberOf:1.2.840.113556.1.4.1941:=<DN>`
  (LDAP_MATCHING_RULE_IN_CHAIN) — пользователи, вложенные группы и
  внешние участники (foreignSecurityPrincipal, cn = SID);
- группа по имени (sAMAccountName) — для share_readers и local_groups.

Членство через основную группу (primaryGroupID, обычно Domain Users)
в атрибуте member не хранится и правилом IN_CHAIN не находится: такие
пользователи добавляются по primaryGroupID = RID группы. Domain Users и
группы, в которые вложен широкий SID (Everyone, Authenticated Users,
Domain Users), раскрываются во всех включённых пользователей домена.

BUILTIN\\* (S-1-5-32-…) — локальные группы файлового сервера: их состав
на рядовом сервере не совпадает с одноимёнными группами контроллера, и
без сопоставления в `[local_groups]` они не вычисляются (разрешение по
ним не засчитывается, запрет — для всех).

Кэш SID → пользователи живёт один обход: создайте новый каталог на
следующий запуск, чтобы увидеть изменения в группах.
"""

import contextlib
import logging
import ssl
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from kronto_agent.config import LdapConfig
from kronto_agent.rights import NEVER_IN_NETWORK_TOKEN, WIDE_SIDS
from kronto_agent.security import sid_to_bytes, sid_to_str

log = logging.getLogger(__name__)

IN_CHAIN = "1.2.840.113556.1.4.1941"
BIT_AND = "1.2.840.113556.1.4.803"
UAC_ACCOUNTDISABLE = 0x2
DOMAIN_USERS_RID = 513
BUILTIN_PREFIX = "S-1-5-32-"

# Одна запись LDAP: DN и сырые атрибуты (имя в нижнем регистре → значения).
Entry = tuple[str, dict[str, list[bytes]]]
# search(base, scope, filter, attributes) → записи; scope: "base"|"subtree".
LdapSearch = Callable[[str, str, str, list[str]], list[Entry]]


class DirectoryError(RuntimeError):
    """Каталог недоступен или отвечает не так, как ждём."""


def escape_filter_value(value: str | bytes) -> str:
    """RFC 4515: экранирование значения в фильтре; байты — целиком \\xx."""
    if isinstance(value, bytes):
        return "".join(f"\\{b:02x}" for b in value)
    out = []
    for ch in value:
        if ch in "*()\\\0":
            out.append(f"\\{ord(ch):02x}")
        else:
            out.append(ch)
    return "".join(out)


@dataclass(frozen=True)
class User:
    sid: str
    dn: str
    email: str | None
    primary_group_rid: int | None


def _first(attrs: dict[str, list[bytes]], name: str) -> bytes | None:
    values = attrs.get(name.lower())
    return values[0] if values else None


def _text(attrs: dict[str, list[bytes]], name: str) -> str | None:
    value = _first(attrs, name)
    return value.decode("utf-8", "replace").strip() if value else None


def _int(attrs: dict[str, list[bytes]], name: str) -> int | None:
    value = _text(attrs, name)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _classes(attrs: dict[str, list[bytes]]) -> set[str]:
    return {v.decode("ascii", "replace").lower() for v in attrs.get("objectclass", [])}


def _sid(attrs: dict[str, list[bytes]]) -> str | None:
    raw = _first(attrs, "objectSid")
    if not raw:
        return None
    try:
        return sid_to_str(raw)[0]
    except ValueError:
        return None


class AdDirectory:
    """Каталог AD поверх функции поиска (ldap3 или поддельной в тестах)."""

    def __init__(
        self,
        search: LdapSearch,
        base_dn: str,
        *,
        upn_fallback: bool = False,
        local_groups: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._search = search
        self._base = base_dn
        self._upn_fallback = upn_fallback
        self._local_groups = local_groups or {}
        self._cache: dict[str, frozenset[str] | None] = {}
        self.domain_sid = self._load_domain_sid()
        self.users = self._load_users()
        self._all = frozenset(self.users)

    # --- Загрузка ---------------------------------------------------------

    def _load_domain_sid(self) -> str:
        found = self._search(self._base, "base", "(objectClass=*)", ["objectSid"])
        sid = _sid(found[0][1]) if found else None
        if not sid:
            raise DirectoryError(
                f"у {self._base} нет objectSid — это не корень домена?"
            )
        return sid

    def _load_users(self) -> dict[str, User]:
        flt = (
            "(&(objectCategory=person)(objectClass=user)"
            f"(!(userAccountControl:{BIT_AND}:={UAC_ACCOUNTDISABLE})))"
        )
        attrs = ["objectSid", "mail", "userPrincipalName", "primaryGroupID"]
        users: dict[str, User] = {}
        for dn, entry in self._search(self._base, "subtree", flt, attrs):
            sid = _sid(entry)
            if not sid:
                continue
            mail = _text(entry, "mail")
            if not mail and self._upn_fallback:
                mail = _text(entry, "userPrincipalName")
            users[sid] = User(
                sid=sid,
                dn=dn,
                email=mail.lower() if mail else None,
                primary_group_rid=_int(entry, "primaryGroupID"),
            )
        log.info("каталог: включённых пользователей %d", len(users))
        return users

    # --- Directory (rights.py) -------------------------------------------

    def all_users(self) -> frozenset[str]:
        return self._all

    def email(self, user_sid: str) -> str | None:
        user = self.users.get(user_sid)
        return user.email if user else None

    def members(self, sid: str) -> frozenset[str] | None:
        sid = sid.upper()
        if sid not in self._cache:
            self._cache[sid] = self._resolve(sid)
        return self._cache[sid]

    # --- Разрешение SID ---------------------------------------------------

    def _is_wide(self, sid: str) -> bool:
        return sid in WIDE_SIDS or sid == f"{self.domain_sid}-{DOMAIN_USERS_RID}"

    def _resolve(self, sid: str) -> frozenset[str] | None:
        if self._is_wide(sid):
            return self._all
        if sid in NEVER_IN_NETWORK_TOKEN:
            return frozenset()
        if sid in self._local_groups:
            return self._resolve_local(sid)
        if sid.startswith(BUILTIN_PREFIX):
            # BUILTIN\* — локальные группы того сервера, где лежат файлы; в
            # LDAP контроллера — другие группы с тем же SID. Без
            # сопоставления в [local_groups] не вычисляется.
            return None
        if sid in self.users:
            return frozenset({sid})
        found = self._search(
            self._base,
            "subtree",
            f"(objectSid={escape_filter_value(sid_to_bytes(sid))})",
            ["objectClass", "objectSid"],
        )
        if not found:
            log.debug("SID %s не найден в каталоге", sid)
            return None
        dn, attrs = found[0]
        classes = _classes(attrs)
        if "group" in classes:
            return self._group_members(sid, dn)
        if "foreignsecurityprincipal" in classes:
            return None
        # Отключённый пользователь, компьютер, прочие объекты — читателей нет.
        return frozenset()

    def _group_members(self, group_sid: str, group_dn: str) -> frozenset[str] | None:
        found = self._search(
            self._base,
            "subtree",
            f"(memberOf:{IN_CHAIN}:={escape_filter_value(group_dn)})",
            ["objectClass", "objectSid", "cn"],
        )
        users: set[str] = set()
        groups = {group_sid}
        for _dn, attrs in found:
            classes = _classes(attrs)
            if "foreignsecurityprincipal" in classes:
                # Внешний участник: cn — его SID (S-1-5-11, SID чужого домена…).
                fsp = _text(attrs, "cn") or ""
                if self._is_wide(fsp.upper()):
                    return self._all
                continue
            sid = _sid(attrs)
            if not sid:
                continue
            if "group" in classes:
                if self._is_wide(sid):
                    return self._all
                groups.add(sid)
            elif sid in self.users:
                users.add(sid)
        # Основная группа: членство не в member, а в primaryGroupID.
        rids = {
            int(g.rsplit("-", 1)[1])
            for g in groups
            if g.startswith(self.domain_sid + "-")
        }
        users.update(u.sid for u in self.users.values() if u.primary_group_rid in rids)
        return frozenset(users)

    def _resolve_local(self, sid: str) -> frozenset[str] | None:
        result: set[str] = set()
        for name in self._local_groups[sid]:
            target = name if name.upper().startswith("S-1-") else self.sid_by_name(name)
            members = self.members(target) if target else None
            if members is None:
                return None
            result |= members
        return frozenset(result)

    def sid_by_name(self, name: str) -> str | None:
        """SID группы или пользователя по sAMAccountName (DOMAIN\\ отбрасывается)."""
        if name.upper().startswith("S-1-"):
            return name.upper()
        account = name.split("\\")[-1]
        found = self._search(
            self._base,
            "subtree",
            f"(sAMAccountName={escape_filter_value(account)})",
            ["objectSid"],
        )
        return _sid(found[0][1]) if found else None

    def emails(self, user_sids: Iterable[str]) -> tuple[set[str], int]:
        """Почты пользователей и число пользователей без почты."""
        out: set[str] = set()
        missing = 0
        for sid in user_sids:
            email = self.email(sid)
            if email:
                out.add(email)
            else:
                missing += 1
        return out, missing


# --- ldap3 -------------------------------------------------------------------


def ldap3_search(cfg: LdapConfig) -> tuple[LdapSearch, Callable[[], None]]:
    """Подключение к контроллеру домена: функция поиска и закрытие."""
    import ldap3
    from ldap3.core.exceptions import LDAPException

    tls = ldap3.Tls(
        validate=ssl.CERT_REQUIRED,
        ca_certs_file=cfg.ca_file,
    )
    server = ldap3.Server(
        cfg.host,
        port=cfg.port,
        use_ssl=cfg.use_ssl,
        tls=tls,
        get_info=ldap3.NONE,
        connect_timeout=15,
    )
    try:
        conn = ldap3.Connection(
            server,
            user=cfg.bind_dn,
            password=cfg.password,
            authentication=ldap3.SIMPLE,
            read_only=True,
            raise_exceptions=True,
            receive_timeout=60,
            auto_bind=(
                ldap3.AUTO_BIND_TLS_BEFORE_BIND
                if cfg.start_tls
                else ldap3.AUTO_BIND_NO_TLS
            ),
        )
    except LDAPException as exc:
        raise DirectoryError(f"LDAP {cfg.host}:{cfg.port}: {exc}") from exc

    scopes = {"base": ldap3.BASE, "subtree": ldap3.SUBTREE}

    def search(base: str, scope: str, flt: str, attributes: list[str]) -> list[Entry]:
        out: list[Entry] = []
        try:
            for item in conn.extend.standard.paged_search(
                search_base=base,
                search_filter=flt,
                search_scope=scopes[scope],
                attributes=attributes,
                paged_size=500,
                generator=True,
            ):
                if item.get("type") != "searchResEntry":
                    continue  # ссылки (referral) на другие разделы
                raw: dict[str, Any] = item.get("raw_attributes") or {}
                out.append((item["dn"], {k.lower(): list(v) for k, v in raw.items()}))
        except LDAPException as exc:
            raise DirectoryError(f"LDAP-поиск {flt}: {exc}") from exc
        return out

    def close() -> None:
        with contextlib.suppress(LDAPException):
            conn.unbind()

    return search, close
