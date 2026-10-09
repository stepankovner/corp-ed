"""Чёрный ящик: компании, членство, люди, админка, тариф (ТЗ §2, §4, §5, §7, §10).

Написано только по ТЗ, схеме API и описанию тестовой среды (HARNESS.md);
код продукта не читался.

Общие допущения (HARNESS, правило 3) — точных кодов отказов ТЗ не задаёт:
- «без входа» — 401 или 403;
- «чужая роль / чужая компания» — 403 или 404 (данных нет);
- «доступ к компании отозван» (убран, заблокирован, ушёл) — 401, 403 или 404;
- бизнес-отказ (места, приглашение, последний админ) — любой 4xx, и всегда
  в паре с проверкой, что состояние не изменилось;
- нарушение схемы запроса — 422 (так описаны все операции схемы API);
- id для /users/{id} берётся из GET /users, а для /people/{id} — из
  GET /people или /auth/me (company.member_id): схема не говорит, что это
  один и тот же идентификатор.
"""

from __future__ import annotations

import os
import re
import struct
import uuid
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from tests.blackbox.conftest import API, Kronto

PASSWORD = "Sotrudnik-Kr0nto!2026"
ADMIN_TEMP_PASSWORD = "Vremenny-Parol!2026"
ADMIN_PASSWORD = "Zx9-Polar-Lantern!77"

UNAUTHENTICATED = (401, 403)
FORBIDDEN = (403, 404)
NO_ACCESS = (401, 403, 404)

GENERAL_OR_REFUSAL_PREFIX = "В документах компании ответа нет"


# ---------------------------------------------------------------- участники


@dataclass
class Actor:
    """Человек со своим браузером; access-токен — в заголовке Authorization."""

    email: str
    password: str
    http: httpx.AsyncClient
    totp: str | None = None
    tenant_id: str | None = None

    def use(self, token: str) -> None:
        self.http.headers["Authorization"] = f"Bearer {token}"

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.http.get(API + path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.http.post(API + path, **kwargs)

    async def patch(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.http.patch(API + path, **kwargs)

    async def put(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.http.put(API + path, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.http.delete(API + path, **kwargs)

    async def me(self) -> dict[str, Any]:
        r = await self.get("/auth/me")
        assert r.status_code == 200, r.text
        return r.json()

    async def refresh(self) -> None:
        """Новый access-токен по cookie сеанса — как браузер после 401.
        Разбор 06.10: смена роли гасит прежний access-токен (права меняются
        сразу), сеанс остаётся — обновление выдаёт токен с новой ролью."""
        r = await self.post("/auth/refresh")
        assert r.status_code == 200, r.text
        self.use(r.json()["access_token"])

    async def member_id(self) -> str:
        me = await self.me()
        assert me["company"] is not None, me
        return me["company"]["member_id"]


_CODE = re.compile(r"\b(\d{6})\b")


def _code_in(letter: Any) -> str | None:
    for part in (letter.subject, letter.text):
        if part:
            match = _CODE.search(part)
            if match:
                return match.group(1)
    return None


def code_from(letter: Any) -> str:
    code = _code_in(letter)
    assert code, f"в письме нет кода из 6 цифр: {letter.subject!r}"
    return code


async def _fresh_code(kronto: Kronto, email: str, seen: int) -> str | None:
    letters = await kronto.inbox(email)
    for letter in reversed(letters[seen:]):
        code = _code_in(letter)
        if code:
            return code
    return None


async def login_request(
    http: httpx.AsyncClient, email: str, password: str, *, remember: bool = True
) -> dict[str, Any]:
    """Первый шаг входа (ТЗ §3): почта + пароль, без кода компании."""
    http.headers.pop("Authorization", None)
    r = await http.post(
        f"{API}/auth/login",
        json={"email": email, "password": password, "remember": remember},
    )
    assert r.status_code == 200, r.text
    return r.json()


async def login(
    kronto: Kronto,
    email: str,
    password: str,
    *,
    totp: str | None = None,
    http: httpx.AsyncClient | None = None,
    remember: bool = True,
) -> httpx.AsyncClient:
    """Полный вход: пароль, затем второй фактор (приложение или код из письма)."""
    http = http if http is not None else kronto.browser()
    seen = len(await kronto.inbox(email))
    body = await login_request(http, email, password, remember=remember)
    if body["status"] == "ok":
        token = body["access_token"]
    else:
        mfa = body["mfa"]
        if totp is not None and "totp" in mfa["methods"]:
            payload = {
                "token": mfa["token"],
                "method": "totp",
                "code": kronto.totp(totp),
            }
        else:
            assert "email" in mfa["methods"], mfa
            code = await _fresh_code(kronto, email, seen)
            if code is None:
                r = await http.post(
                    f"{API}/auth/mfa/resend", json={"token": mfa["token"]}
                )
                assert r.status_code == 202, r.text
                code = await _fresh_code(kronto, email, seen)
            assert code, "код входа не пришёл на почту"
            payload = {"token": mfa["token"], "method": "email", "code": code}
        r = await http.post(f"{API}/auth/mfa/verify", json=payload)
        assert r.status_code == 200, r.text
        token = r.json()["access_token"]
    http.headers["Authorization"] = f"Bearer {token}"
    return http


async def new_company(
    kronto: Kronto,
    code: str,
    *,
    name: str | None = None,
    email: str | None = None,
    seats: int = 10,
    tariff: str = "base",
    not_found_mode: str = "general",
) -> Actor:
    """Компания от команды kronto и её администратор, вошедший с приложением.

    Администраторам второй фактор — только приложение или ключ (ТЗ §3).
    Временный пароль меняется сразу после входа, если сервер этого требует.
    """
    email = email or f"admin@{code}.ru"
    created = await kronto.create_company(
        code=code,
        name=name or f"Компания {code}",
        admin_email=email,
        admin_password=ADMIN_TEMP_PASSWORD,
        seats=seats,
        tariff=tariff,
        not_found_mode=not_found_mode,
    )
    secret = await kronto.enable_totp(email)
    http = await login(kronto, email, ADMIN_TEMP_PASSWORD, totp=secret)
    admin = Actor(
        email=email,
        password=ADMIN_TEMP_PASSWORD,
        http=http,
        totp=secret,
        tenant_id=created["id"],
    )
    if (await admin.me())["must_change_password"]:
        r = await admin.post(
            "/auth/change-password",
            json={
                "current_password": ADMIN_TEMP_PASSWORD,
                "new_password": ADMIN_PASSWORD,
            },
        )
        assert r.status_code == 200, r.text
        admin.use(r.json()["access_token"])
        admin.password = ADMIN_PASSWORD
    return admin


async def register(
    kronto: Kronto,
    email: str,
    *,
    first: str = "Иван",
    last: str = "Петров",
    invite: str | None = None,
) -> Actor:
    """Регистрация (ТЗ §2) и подтверждение почты кодом из письма (ТЗ §3)."""
    http = kronto.browser()
    payload: dict[str, Any] = {
        "first_name": first,
        "last_name": last,
        "email": email,
        "password": PASSWORD,
        "terms": True,
        "consent": True,
    }
    if invite is not None:
        payload["invite"] = invite
    r = await http.post(f"{API}/auth/register", json=payload)
    assert r.status_code == 202, r.text
    letter = await kronto.last_letter(email)
    r = await http.post(
        f"{API}/auth/verify-email", json={"email": email, "code": code_from(letter)}
    )
    assert r.status_code == 200, r.text
    person = Actor(email=email, password=PASSWORD, http=http)
    person.use(r.json()["access_token"])
    return person


async def ensure_session(kronto: Kronto, actor: Actor) -> None:
    """Если после ухода из компании старый токен отозван — восстановить сеанс
    обычным путём (refresh-cookie или вход заново): учётка ведь осталась."""
    r = await actor.get("/auth/me")
    if r.status_code == 200:
        return
    actor.http.headers.pop("Authorization", None)
    r = await actor.post("/auth/refresh")
    if r.status_code == 200:
        actor.use(r.json()["access_token"])
        return
    await login(kronto, actor.email, actor.password, totp=actor.totp, http=actor.http)


async def make_invite(admin: Actor, **options: Any) -> dict[str, Any]:
    r = await admin.post("/invites", json=options)
    assert r.status_code == 201, r.text
    return r.json()


async def accept(person: Actor, secret: str) -> httpx.Response:
    """Вступить по ссылке или коду; вступил — сессия переключается на компанию."""
    r = await person.post("/invites/accept", json={"secret": secret})
    if r.status_code == 200 and r.json().get("session"):
        person.use(r.json()["session"]["access_token"])
    return r


async def hire(
    kronto: Kronto,
    admin: Actor,
    email: str,
    *,
    first: str = "Иван",
    last: str = "Петров",
    secret: str | None = None,
) -> Actor:
    """Сотрудник: регистрация и вступление по коду приглашения."""
    if secret is None:
        secret = (await make_invite(admin))["code"]
    person = await register(kronto, email, first=first, last=last)
    r = await accept(person, secret)
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "joined", r.json()
    return person


async def switch(actor: Actor, tenant_id: str) -> httpx.Response:
    r = await actor.post("/auth/switch-company", json={"tenant_id": tenant_id})
    if r.status_code == 200:
        actor.use(r.json()["access_token"])
    return r


async def users(admin: Actor) -> list[dict[str, Any]]:
    r = await admin.get("/users")
    assert r.status_code == 200, r.text
    return r.json()


async def user_by_email(admin: Actor, email: str) -> dict[str, Any] | None:
    for user in await users(admin):
        if user["email"] == email:
            return user
    return None


async def user_id(admin: Actor, email: str) -> str:
    user = await user_by_email(admin, email)
    assert user is not None, f"{email} нет в /users"
    return user["id"]


async def people_emails(actor: Actor) -> set[str]:
    r = await actor.get("/people")
    assert r.status_code == 200, r.text
    return {p["email"] for p in r.json()}


async def invite_state(admin: Actor, invite_id: str) -> dict[str, Any]:
    r = await admin.get("/invites")
    assert r.status_code == 200, r.text
    found = [i for i in r.json() if i["id"] == invite_id]
    assert len(found) == 1, r.json()
    return found[0]


async def company(admin: Actor) -> dict[str, Any]:
    r = await admin.get("/company")
    assert r.status_code == 200, r.text
    return r.json()


def active_tenants(me: dict[str, Any]) -> set[str]:
    return {c["tenant_id"] for c in me["companies"] if c["status"] == "active"}


def assert_refused(r: httpx.Response) -> None:
    # допущение: бизнес-отказ — любой 4xx; состояние проверяется отдельно.
    assert 400 <= r.status_code < 500, (r.status_code, r.text)


def assert_not_joined(r: httpx.Response) -> None:
    # допущение: отказ — 4xx или ответ 200 без вступления (не «joined»).
    if r.status_code == 200:
        assert r.json()["outcome"] != "joined", r.json()
    else:
        assert_refused(r)


Call = Callable[[], Awaitable[httpx.Response]]


async def assert_all_forbidden(*calls: Call) -> None:
    """Каждый вызов — отказ «чужая роль / чужая компания» (403 или 404)."""
    for call in calls:
        r = await call()
        assert r.status_code in FORBIDDEN, (
            r.request.method,
            str(r.request.url),
            r.status_code,
            r.text,
        )


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ---------------------------------------------------------------- картинки


def png(width: int = 320, height: int = 320, *, noise: bool = False) -> bytes:
    """Настоящий PNG. noise=True — несжимаемый шум (файл больше 5 МБ при 1500×1500)."""
    if noise:
        raw = b"".join(b"\x00" + os.urandom(width * 3) for _ in range(height))
        data = zlib.compress(raw, 0)
    else:
        row = b"\x00" + bytes((59, 52, 214)) * width
        data = zlib.compress(row * height, 9)

    def chunk(tag: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(tag + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", data)
        + chunk(b"IEND", b"")
    )


def split_url(url: str) -> tuple[str, dict[str, str]]:
    """Подписанная ссылка (логотип, фото) → путь и параметры для клиента теста."""
    parts = urlsplit(url)
    path = parts.path
    if not path.startswith(API):
        path = API + path  # допущение: ссылка может быть дана относительно API
    return path, dict(parse_qsl(parts.query))


# ---------------------------------------------------------------- коннекторы


async def connector_kinds(admin: Actor) -> list[dict[str, Any]]:
    r = await admin.get("/connectors/kinds")
    assert r.status_code == 200, r.text
    kinds = r.json()
    assert kinds, "каталог коннекторов пуст"
    return kinds


def _sample_value(kind: str, field: str, variant: int = 0) -> str:
    # допущение: правдоподобные значения полей формы по их названию из каталога;
    # variant делает значения разными, чтобы подключения не считались дублями.
    name, kind = field.lower(), kind.lower()
    tag = f"kronto-test{variant}" if variant else "kronto-test"
    if any(
        w in name
        for w in (
            "url",
            "portal",
            "domain",
            "host",
            "site",
            "address",
            "endpoint",
            "server",
        )
    ):
        if "bitrix" in kind:
            return f"https://{tag}.bitrix24.ru"
        if "confluence" in kind:
            return f"https://{tag}.atlassian.net/wiki"
        return f"https://{tag}.ru"
    if "mail" in name or "login" in name or "user" in name:
        return f"robot@{tag}.ru"
    if "space" in name:
        return f"KB{variant}" if variant else "KB"
    return tag


def connector_payload(
    spec: dict[str, Any], name: str, variant: int = 0
) -> dict[str, Any]:
    config = {
        f["name"]: _sample_value(spec["kind"], f["name"], variant)
        for f in spec["config_fields"]
        if f["required"]
    }
    return {
        "kind": spec["kind"],
        "name": name,
        "modules": [spec["modules"][0]["name"]],
        "config": config,
    }


def creatable(
    kinds: list[dict[str, Any]], *, prefer_mode: str | None = None
) -> dict[str, Any]:
    """Доступный по тарифу вид с наименьшим числом обязательных полей."""
    candidates = [k for k in kinds if k.get("available", True) and k["modules"]]
    assert candidates, kinds
    if prefer_mode is not None:
        preferred = [k for k in candidates if k["mode"] == prefer_mode]
        candidates = preferred or candidates
    return min(
        candidates,
        key=lambda k: (
            _own_address(k),
            sum(1 for f in k["config_fields"] if f["required"]),
        ),
    )


def _own_address(spec: dict[str, Any]) -> bool:
    """Нужен адрес своей установки (Nextcloud, NAS): выдуманное имя не
    резолвится, и создание отклонят проверкой адреса — такие виды в конец.
    У Битрикс24 и Confluence облачные домены с wildcard-DNS."""
    for field in spec["config_fields"]:
        if not field["required"]:
            continue
        value = _sample_value(spec["kind"], field["name"])
        if value.startswith("https://") and not value.endswith(
            (".bitrix24.ru", ".atlassian.net/wiki")
        ):
            return True
    return False


async def create_connector(
    admin: Actor, spec: dict[str, Any], name: str, variant: int = 0
) -> dict[str, Any]:
    r = await admin.post("/connectors", json=connector_payload(spec, name, variant))
    assert r.status_code == 201, r.text
    return r.json()


# === приглашения


async def test_invite_returns_link_and_short_code(kronto: Kronto) -> None:
    """ТЗ §2, §7: приглашение — ссылка и код (короткая форма той же ссылки,
    чтобы продиктовать); галочка одобрения по умолчанию выключена; в списке
    приглашений секретов нет."""
    admin = await new_company(kronto, "alpha")
    created = await make_invite(admin)
    token, code = created["token"], created["code"]
    assert token and code
    # допущение: «короткая форма» — короче ссылки, из букв, цифр и дефисов
    # (пример ТЗ — K7QM-4XPA); не короче 8 знаков — минимум секрета по схеме.
    assert len(code) < len(token)
    assert re.fullmatch(r"[A-Za-z0-9-]{8,16}", code), code
    invite = created["invite"]
    assert invite["requires_approval"] is False
    assert invite["status"] == "active"
    assert invite["uses"] == 0
    assert invite["email_domain"] is None

    r = await admin.get("/invites")
    assert r.status_code == 200, r.text
    assert [i["id"] for i in r.json()].count(invite["id"]) == 1
    assert token not in r.text
    assert code not in r.text


async def test_invite_preview_before_login_by_link_and_code(kronto: Kronto) -> None:
    """ТЗ §2: по ссылке или коду видно, в какую компанию ведёт приглашение, —
    ещё до входа; неизвестный код ничего не раскрывает."""
    admin = await new_company(kronto, "alpha", name="ООО Альфа")
    created = await make_invite(admin)
    guest = kronto.browser()
    for secret in (created["token"], created["code"]):
        r = await guest.post(f"{API}/invites/preview", json={"secret": secret})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["company_name"] == "ООО Альфа"
        assert body["requires_approval"] is False
        assert body["email_domain"] is None

    r = await guest.post(f"{API}/invites/preview", json={"secret": "ZZZZ-ZZZZ"})
    assert_refused(r)
    assert "ООО Альфа" not in r.text


async def test_join_by_code_switches_session_to_company(kronto: Kronto) -> None:
    """ТЗ §2: человек без компании вступает по коду приглашения; сессия сразу
    переключается на компанию, роль — сотрудник, он виден коллегам."""
    admin = await new_company(kronto, "alpha", name="ООО Альфа")
    created = await make_invite(admin)
    person = await register(kronto, "ivan@alpha.ru")
    before = await person.me()
    assert before["company"] is None
    assert before["companies"] == []

    r = await accept(person, created["code"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "joined"
    assert body["company_name"] == "ООО Альфа"
    assert body["session"] is not None

    me = await person.me()
    assert me["company"]["tenant_id"] == admin.tenant_id
    assert me["company"]["role"] == "employee"
    assert admin.tenant_id in active_tenants(me)
    assert "ivan@alpha.ru" in await people_emails(person)
    assert "ivan@alpha.ru" in await people_emails(admin)
    assert (await invite_state(admin, created["invite"]["id"]))["uses"] == 1


async def test_join_by_link_and_repeat_is_already_member(kronto: Kronto) -> None:
    """ТЗ §2: вступление по ссылке-приглашению; повторное вступление не
    создаёт второго членства."""
    admin = await new_company(kronto, "alpha")
    created = await make_invite(admin, max_uses=10)
    person = await register(kronto, "ivan@alpha.ru")

    r = await accept(person, created["token"])
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "joined"
    r = await accept(person, created["token"])
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "already_member"

    assert [u["email"] for u in await users(admin)].count("ivan@alpha.ru") == 1
    assert (await person.me())["company"]["tenant_id"] == admin.tenant_id


async def test_company_code_is_not_an_invite(kronto: Kronto) -> None:
    """ТЗ §2: код компании для вступления не годится — он не секрет."""
    admin = await new_company(kronto, "northwind")
    company_code = (await company(admin))["company_code"]
    person = await register(kronto, "ivan@northwind.ru")
    guest = kronto.browser()

    for secret in (company_code, company_code.upper()):
        r = await guest.post(f"{API}/invites/preview", json={"secret": secret})
        assert_refused(r)
        r = await accept(person, secret)
        assert_refused(r)

    me = await person.me()
    assert me["company"] is None
    assert admin.tenant_id not in active_tenants(me)
    assert await user_by_email(admin, person.email) is None


async def test_revoked_invite_stops_working(kronto: Kronto) -> None:
    """ТЗ §2: приглашение можно отозвать — после отзыва ни ссылка, ни код
    не работают."""
    admin = await new_company(kronto, "alpha")
    created = await make_invite(admin)
    r = await admin.delete(f"/invites/{created['invite']['id']}")
    assert r.status_code == 204, r.text
    assert (await invite_state(admin, created["invite"]["id"]))["status"] == "revoked"

    guest = kronto.browser()
    r = await guest.post(f"{API}/invites/preview", json={"secret": created["token"]})
    assert_refused(r)

    person = await register(kronto, "ivan@alpha.ru")
    for secret in (created["code"], created["token"]):
        r = await accept(person, secret)
        assert_refused(r)
    me = await person.me()
    assert me["company"] is None
    assert await user_by_email(admin, person.email) is None


async def test_invite_max_uses_is_enforced(kronto: Kronto) -> None:
    """ТЗ §2: у приглашения — число людей; сверх него не вступить."""
    admin = await new_company(kronto, "alpha")
    created = await make_invite(admin, max_uses=1)
    assert created["invite"]["max_uses"] == 1
    await hire(kronto, admin, "first@alpha.ru", secret=created["code"])

    second = await register(kronto, "second@alpha.ru")
    r = await accept(second, created["code"])
    assert_refused(r)
    me = await second.me()
    assert me["company"] is None
    assert admin.tenant_id not in active_tenants(me)

    state = await invite_state(admin, created["invite"]["id"])
    assert state["uses"] == 1
    assert state["status"] == "used_up"
    assert await user_by_email(admin, second.email) is None


async def test_invite_email_domain_is_enforced(kronto: Kronto) -> None:
    """ТЗ §2: у приглашения — домен почты; с чужим доменом не вступить."""
    admin = await new_company(kronto, "alpha")
    created = await make_invite(admin, email_domain="alpha.ru", max_uses=10)
    assert created["invite"]["email_domain"] == "alpha.ru"
    r = await kronto.browser().post(
        f"{API}/invites/preview", json={"secret": created["code"]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["email_domain"] == "alpha.ru"

    stranger = await register(kronto, "ivan@beta.ru")
    r = await accept(stranger, created["code"])
    assert_refused(r)
    assert (await stranger.me())["company"] is None

    insider = await register(kronto, "anna@alpha.ru", first="Анна")
    r = await accept(insider, created["code"])
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "joined"
    assert await people_emails(admin) == {admin.email, "anna@alpha.ru"}


async def test_invite_with_approval_waits_for_admin(kronto: Kronto) -> None:
    """ТЗ §2, §7: «требовать одобрения администратора» — вступивший ждёт, доступа
    к компании у него нет; админ видит заявку и одобряет — доступ появляется."""
    admin = await new_company(kronto, "alpha")
    created = await make_invite(admin, requires_approval=True)
    r = await kronto.browser().post(
        f"{API}/invites/preview", json={"secret": created["code"]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["requires_approval"] is True

    person = await register(kronto, "ivan@alpha.ru")
    r = await accept(person, created["code"])
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "pending"

    me = await person.me()
    assert me["company"] is None
    assert admin.tenant_id not in active_tenants(me)
    r = await person.get("/people")
    assert r.status_code in NO_ACCESS, r.text
    r = await switch(person, admin.tenant_id)
    assert_refused(r)

    pending = await user_by_email(admin, person.email)
    assert pending is not None and pending["status"] == "pending"
    assert person.email not in await people_emails(admin)

    r = await admin.post(f"/users/{pending['id']}/approve")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"

    assert admin.tenant_id in active_tenants(await person.me())
    r = await switch(person, admin.tenant_id)
    assert r.status_code == 200, r.text
    assert person.email in await people_emails(person)


async def test_rejected_join_request_gives_no_access(kronto: Kronto) -> None:
    """ТЗ §7: заявку на вступление админ может отклонить — доступа нет."""
    admin = await new_company(kronto, "alpha")
    created = await make_invite(admin, requires_approval=True)
    person = await register(kronto, "ivan@alpha.ru")
    r = await accept(person, created["code"])
    assert r.status_code == 200 and r.json()["outcome"] == "pending", r.text
    pending_id = await user_id(admin, person.email)

    r = await admin.post(f"/users/{pending_id}/reject")
    assert r.status_code == 204, r.text

    after = await user_by_email(admin, person.email)
    assert after is None or after["status"] not in ("active", "pending")
    assert admin.tenant_id not in active_tenants(await person.me())
    r = await switch(person, admin.tenant_id)
    assert_refused(r)
    r = await person.get("/people")
    assert r.status_code in NO_ACCESS


async def test_employee_cannot_manage_invites(kronto: Kronto) -> None:
    """ТЗ §2: людьми компании занимается администратор — сотрудник не создаёт,
    не видит и не отзывает приглашения."""
    admin = await new_company(kronto, "alpha")
    created = await make_invite(admin)
    employee = await hire(kronto, admin, "ivan@alpha.ru")

    r = await employee.post("/invites", json={})
    assert r.status_code in FORBIDDEN, r.text
    r = await employee.get("/invites")
    assert r.status_code in FORBIDDEN, r.text
    assert created["invite"]["id"] not in r.text
    r = await employee.delete(f"/invites/{created['invite']['id']}")
    assert r.status_code in FORBIDDEN, r.text

    assert (await invite_state(admin, created["invite"]["id"]))["status"] == "active"


async def test_invite_input_validation(kronto: Kronto) -> None:
    """ТЗ §2: срок и число людей у приглашения ограничены; свой код задать
    нельзя; неверный секрет не принимается."""
    admin = await new_company(kronto, "alpha")
    before = len((await admin.get("/invites")).json())
    for bad in (
        {"ttl_days": 0},
        {"ttl_days": 31},
        {"max_uses": 0},
        {"max_uses": 1001},
        {"email_domain": "a" * 254},
        {"code": "MYCODE-01"},
    ):
        r = await admin.post("/invites", json=bad)
        assert r.status_code == 422, (bad, r.text)
    assert len((await admin.get("/invites")).json()) == before

    edge = await make_invite(admin, ttl_days=30, max_uses=1000)
    assert edge["invite"]["max_uses"] == 1000

    person = await register(kronto, "ivan@alpha.ru")
    for secret in ("short", "x" * 129):
        r = await accept(person, secret)
        assert r.status_code == 422, r.text
    r = await accept(person, "QQQQ-QQQQ")
    assert_refused(r)
    assert (await person.me())["company"] is None


async def test_registration_with_invite_joins_company(kronto: Kronto) -> None:
    """ТЗ §2: регистрация по ссылке-приглашению — после подтверждения почты
    человек в компании."""
    admin = await new_company(kronto, "alpha")
    created = await make_invite(admin)
    person = await register(kronto, "ivan@alpha.ru", invite=created["token"])
    # Разбор 06.10: поле invite при регистрации только пропускает при
    # закрытой регистрации; вступление — отдельный шаг «Вступить» (схема
    # API теперь это описывает; ТЗ §2 автовступления не требует).
    assert (await person.me())["company"] is None
    r = await accept(person, created["token"])
    assert r.status_code == 200, r.text
    me = await person.me()
    assert admin.tenant_id in active_tenants(me)
    assert person.email in await people_emails(admin)


# === членство и роли


async def test_removed_member_loses_access_immediately(kronto: Kronto) -> None:
    """ТЗ §2, §7: убранный админом теряет доступ к компании сразу (тем же
    токеном), учётка остаётся — он входит и оказывается без компании."""
    admin = await new_company(kronto, "alpha")
    employee = await hire(kronto, admin, "ivan@alpha.ru")
    assert (await employee.get("/people")).status_code == 200

    r = await admin.delete(f"/users/{await user_id(admin, employee.email)}")
    assert r.status_code == 204, r.text

    for path in ("/people", "/departments"):
        r = await employee.get(path)
        assert r.status_code in NO_ACCESS, (path, r.status_code, r.text)
    r = await employee.get("/auth/me")
    if r.status_code == 200:
        me = r.json()
        assert admin.tenant_id not in active_tenants(me)
        assert me["company"] is None or me["company"]["tenant_id"] != admin.tenant_id
    else:
        assert r.status_code == 401, r.text

    left = await user_by_email(admin, employee.email)
    assert left is None or left["status"] == "left"
    assert employee.email not in await people_emails(admin)

    again = Actor(
        employee.email, PASSWORD, await login(kronto, employee.email, PASSWORD)
    )
    me = await again.me()
    assert me["company"] is None
    assert admin.tenant_id not in active_tenants(me)


async def test_blocked_member_loses_access_immediately(kronto: Kronto) -> None:
    """ТЗ §7: блокировка — доступ пропадает сразу; заблокированного нет в
    справочнике; разблокировка возвращает членство."""
    admin = await new_company(kronto, "alpha")
    employee = await hire(kronto, admin, "ivan@alpha.ru")
    uid = await user_id(admin, employee.email)

    r = await admin.patch(f"/users/{uid}", json={"blocked": True})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "blocked"
    r = await employee.get("/people")
    assert r.status_code in NO_ACCESS, r.text
    assert employee.email not in await people_emails(admin)

    r = await admin.patch(f"/users/{uid}", json={"blocked": False})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"
    assert employee.email in await people_emails(admin)


async def test_member_leaves_and_returns_by_new_invite(kronto: Kronto) -> None:
    """ТЗ §2, §4: человек сам выходит из компании — учётка остаётся, доступ к
    компании пропадает; вернуться можно по новому приглашению."""
    admin = await new_company(kronto, "alpha")
    employee = await hire(kronto, admin, "ivan@alpha.ru")

    r = await employee.post("/account/leave", json={"tenant_id": admin.tenant_id})
    assert r.status_code == 204, r.text
    r = await employee.get("/people")
    assert r.status_code in NO_ACCESS, r.text
    gone = await user_by_email(admin, employee.email)
    assert gone is None or gone["status"] == "left"
    assert employee.email not in await people_emails(admin)

    await ensure_session(kronto, employee)
    me = await employee.me()
    assert admin.tenant_id not in active_tenants(me)

    fresh = await make_invite(admin)
    r = await accept(employee, fresh["code"])
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "joined"
    assert (await employee.me())["company"]["tenant_id"] == admin.tenant_id
    assert employee.email in await people_emails(admin)


async def test_last_admin_cannot_step_down(kronto: Kronto) -> None:
    """ТЗ §2: администратор в компании — не меньше одного: единственный админ
    не может уйти, разжаловать, заблокировать или убрать себя."""
    admin = await new_company(kronto, "alpha")
    uid = await user_id(admin, admin.email)

    r = await admin.post("/account/leave", json={"tenant_id": admin.tenant_id})
    assert_refused(r)
    r = await admin.patch(f"/users/{uid}", json={"role": "employee"})
    assert_refused(r)
    r = await admin.patch(f"/users/{uid}", json={"blocked": True})
    assert_refused(r)
    r = await admin.delete(f"/users/{uid}")
    assert_refused(r)

    me = await admin.me()
    assert me["company"]["role"] == "admin"
    assert me["company"]["tenant_id"] == admin.tenant_id
    row = await user_by_email(admin, admin.email)
    assert row is not None and row["role"] == "admin" and row["status"] == "active"


async def test_second_admin_lets_first_step_down(kronto: Kronto) -> None:
    """ТЗ §2, §3: админов может быть несколько; назначенному админу обязателен
    сильный второй фактор; когда админов двое, прежний может стать сотрудником —
    и сразу теряет права администратора."""
    admin = await new_company(kronto, "alpha")
    employee = await hire(kronto, admin, "ivan@alpha.ru")
    employee_uid = await user_id(admin, employee.email)
    assert (await employee.me())["mfa"]["strong_required"] is False

    r = await admin.patch(f"/users/{employee_uid}", json={"role": "admin"})
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "admin"
    await employee.refresh()
    me = await employee.me()
    assert me["company"]["role"] == "admin"
    assert me["mfa"]["strong_required"] is True

    admin_uid = await user_id(admin, admin.email)
    # Разбор 06.10: свою роль не меняет никто (ТЗ об этом молчит; так нельзя
    # случайно лишить себя прав) — разжалует второй админ. Ему, как
    # администратору, нужно приложение (ТЗ §3): включает и входит заново.
    r = await admin.patch(f"/users/{admin_uid}", json={"role": "employee"})
    assert_refused(r)
    secret = await kronto.enable_totp(employee.email)
    employee.http = await login(kronto, employee.email, employee.password, totp=secret)
    r = await employee.patch(f"/users/{admin_uid}", json={"role": "employee"})
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "employee"

    # допущение: роль читается из членства на каждом запросе, а не из
    # выданного раньше токена. Разбор 06.10: прежний токен после смены роли
    # не действует вовсе (401), сеанс — обновлением, уже с новой ролью.
    r = await admin.get("/users")
    assert r.status_code in NO_ACCESS, r.text
    r = await admin.patch(f"/users/{admin_uid}", json={"role": "admin"})
    assert r.status_code in NO_ACCESS, r.text
    await admin.refresh()
    assert (await admin.me())["company"]["role"] == "employee"
    r = await admin.get("/users")
    assert r.status_code in FORBIDDEN, r.text


async def test_employee_cannot_change_roles_or_membership(kronto: Kronto) -> None:
    """ТЗ §2: роли и люди — дело администратора: сотрудник не повышает себя,
    не разжалует, не блокирует и не убирает админа, не видит список людей
    с почтами и статусами."""
    admin = await new_company(kronto, "alpha")
    employee = await hire(kronto, admin, "ivan@alpha.ru")
    employee_uid = await user_id(admin, employee.email)
    admin_uid = await user_id(admin, admin.email)

    await assert_all_forbidden(
        lambda: employee.patch(f"/users/{employee_uid}", json={"role": "admin"}),
        lambda: employee.patch(f"/users/{admin_uid}", json={"role": "employee"}),
        lambda: employee.patch(f"/users/{admin_uid}", json={"blocked": True}),
        lambda: employee.delete(f"/users/{admin_uid}"),
        lambda: employee.get("/users"),
    )

    rows = {u["email"]: u for u in await users(admin)}
    assert rows[employee.email]["role"] == "employee"
    assert rows[admin.email]["role"] == "admin"
    assert rows[admin.email]["status"] == "active"
    assert (await employee.me())["company"]["role"] == "employee"


async def test_role_belongs_to_membership_not_account(kronto: Kronto) -> None:
    """ТЗ §2: роль — свойство членства: в одной компании человек админ, в другой
    сотрудник; права меняются вместе с выбранной компанией."""
    person = await new_company(kronto, "alpha", email="olga@alpha.ru")
    beta_admin = await new_company(kronto, "beta")
    invite = await make_invite(beta_admin)

    r = await accept(person, invite["code"])
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "joined"
    me = await person.me()
    assert me["company"]["tenant_id"] == beta_admin.tenant_id
    assert me["company"]["role"] == "employee"
    roles = {c["tenant_id"]: c["role"] for c in me["companies"]}
    assert roles == {person.tenant_id: "admin", beta_admin.tenant_id: "employee"}

    r = await person.get("/users")
    assert r.status_code in FORBIDDEN, r.text
    r = await person.patch("/company", json={"name": "Захват"})
    assert r.status_code in FORBIDDEN, r.text

    r = await switch(person, person.tenant_id)
    assert r.status_code == 200, r.text
    assert (await person.me())["company"]["role"] == "admin"
    r = await person.get("/users")
    assert r.status_code == 200, r.text
    assert (await company(beta_admin))["name"] != "Захват"


async def test_switch_between_own_companies(kronto: Kronto) -> None:
    """ТЗ §1, §2: человек в нескольких компаниях видит их список и переключается;
    данные берутся из выбранной компании; в чужую компанию не переключиться и
    из неё не «выйти»."""
    person = await new_company(kronto, "alpha", name="Альфа")
    beta = await kronto.create_company(
        code="beta", name="Бета", admin_email=person.email, seats=10
    )
    assert beta["account_created"] is False
    gamma = await kronto.create_company(
        code="gamma",
        name="Гамма",
        admin_email="admin@gamma.ru",
        admin_password=ADMIN_TEMP_PASSWORD,
    )

    me = await person.me()
    names = {c["tenant_id"]: c["company_name"] for c in me["companies"]}
    assert names.get(person.tenant_id) == "Альфа"
    assert names.get(beta["id"]) == "Бета"
    assert gamma["id"] not in names

    r = await switch(person, person.tenant_id)
    assert r.status_code == 200, r.text
    r = await person.post(
        "/glossary",
        json={"term": "ДМС", "expansion": "добровольное медицинское страхование"},
    )
    assert r.status_code == 201, r.text

    r = await switch(person, beta["id"])
    assert r.status_code == 200, r.text
    me = await person.me()
    assert me["company"]["tenant_id"] == beta["id"]
    assert me["company"]["name"] == "Бета"
    assert (await company(person))["id"] == beta["id"]
    r = await person.get("/glossary")
    assert r.status_code == 200, r.text
    assert "ДМС" not in {t["term"] for t in r.json()}

    for foreign in (gamma["id"], str(uuid.uuid4())):
        r = await switch(person, foreign)
        assert_refused(r)
    r = await person.post("/account/leave", json={"tenant_id": gamma["id"]})
    assert_refused(r)
    assert (await company(person))["id"] == beta["id"]

    r = await switch(person, person.tenant_id)
    assert r.status_code == 200, r.text
    assert "ДМС" in {t["term"] for t in (await person.get("/glossary")).json()}


async def test_last_admin_cannot_delete_account(kronto: Kronto) -> None:
    """ТЗ §2: удалить учётку может сам человек; последний администратор
    компании — только после назначения другого; удалённая учётка не входит."""
    admin = await new_company(kronto, "alpha")
    r = await admin.post("/account/delete", json={"password": admin.password})
    assert_refused(r)
    assert (await admin.me())["company"]["role"] == "admin"

    employee = await hire(kronto, admin, "ivan@alpha.ru")
    r = await admin.patch(
        f"/users/{await user_id(admin, employee.email)}", json={"role": "admin"}
    )
    assert r.status_code == 200, r.text

    r = await admin.post("/account/delete", json={"password": "Wrong-Password-123!"})
    assert_refused(r)
    r = await admin.post("/account/delete", json={"password": admin.password})
    assert r.status_code == 204, r.text

    await employee.refresh()
    me = await employee.me()
    assert me["company"]["tenant_id"] == admin.tenant_id
    assert me["company"]["role"] == "admin"
    r = await kronto.browser().post(
        f"{API}/auth/login", json={"email": admin.email, "password": admin.password}
    )
    # допущение: вход в удалённую учётку — отказ 4xx.
    assert_refused(r)


# === места


async def test_seat_limit_blocks_extra_member(kronto: Kronto) -> None:
    """ТЗ §7, §10: места по тарифу — работающих людей не больше мест
    (админ тоже занимает место: ACTIVE «занимает место» по схеме)."""
    admin = await new_company(kronto, "alpha", seats=2)
    created = await make_invite(admin, max_uses=10)
    first = await hire(kronto, admin, "first@alpha.ru", secret=created["code"])

    second = await register(kronto, "second@alpha.ru")
    r = await accept(second, created["code"])
    assert_not_joined(r)
    me = await second.me()
    assert me["company"] is None
    assert admin.tenant_id not in active_tenants(me)

    settings = await company(admin)
    assert settings["seats"] == 2
    assert settings["members"] <= 2
    assert await people_emails(admin) == {admin.email, first.email}


async def test_blocked_member_frees_seat_and_unblock_respects_limit(
    kronto: Kronto,
) -> None:
    """ТЗ §7: заблокированный место не занимает; разблокировать, когда мест
    нет, нельзя."""
    admin = await new_company(kronto, "alpha", seats=2)
    created = await make_invite(admin, max_uses=10)
    first = await hire(kronto, admin, "first@alpha.ru", secret=created["code"])
    first_uid = await user_id(admin, first.email)

    r = await admin.patch(f"/users/{first_uid}", json={"blocked": True})
    assert r.status_code == 200, r.text
    second = await hire(kronto, admin, "second@alpha.ru", secret=created["code"])

    # допущение: разблокировка возвращает человеку место, поэтому сверх мест
    # она запрещена так же, как вступление.
    r = await admin.patch(f"/users/{first_uid}", json={"blocked": False})
    assert_refused(r)
    row = await user_by_email(admin, first.email)
    assert row is not None and row["status"] == "blocked"
    assert await people_emails(admin) == {admin.email, second.email}
    assert (await company(admin))["members"] <= 2


async def test_pending_member_cannot_be_approved_beyond_seats(kronto: Kronto) -> None:
    """ТЗ §7: одобрение заявки на вступление тоже упирается в места по тарифу."""
    admin = await new_company(kronto, "alpha", seats=2)
    first = await hire(kronto, admin, "first@alpha.ru")
    gated = await make_invite(admin, requires_approval=True)

    second = await register(kronto, "second@alpha.ru")
    r = await accept(second, gated["code"])
    if r.status_code == 200 and r.json()["outcome"] == "pending":
        pending_id = await user_id(admin, second.email)
        r = await admin.post(f"/users/{pending_id}/approve")
        assert_refused(r)
        row = await user_by_email(admin, second.email)
        assert row is not None and row["status"] != "active"
    else:
        assert_not_joined(r)

    assert admin.tenant_id not in active_tenants(await second.me())
    assert await people_emails(admin) == {admin.email, first.email}


# === профиль, люди, отделы


async def test_profile_fields_visible_to_colleagues(kronto: Kronto) -> None:
    """ТЗ §4: по желанию — отчество, телефон, Telegram; профиль видят коллеги
    и администраторы компании; пустое значение очищает поле."""
    admin = await new_company(kronto, "alpha")
    employee = await hire(kronto, admin, "anna@alpha.ru", first="Анна", last="Смирнова")

    r = await employee.patch(
        "/account",
        json={
            "patronymic": "Сергеевна",
            "phone": "+7 900 123-45-67",
            "telegram": "@anna_s",
        },
    )
    assert r.status_code == 204, r.text
    me = await employee.me()
    # Разбор 06.10: телефон и Telegram сервер приводит к одному виду (схема
    # API теперь это описывает) — сравниваем приведённые значения.
    assert (me["patronymic"], me["phone"], me["telegram"]) == (
        "Сергеевна",
        "+79001234567",
        "anna_s",
    )

    member = me["company"]["member_id"]
    r = await admin.get(f"/people/{member}")
    assert r.status_code == 200, r.text
    card = r.json()
    assert card["first_name"] == "Анна"
    assert card["last_name"] == "Смирнова"
    assert card["patronymic"] == "Сергеевна"
    assert card["phone"] == "+79001234567"
    assert card["telegram"] == "anna_s"
    assert card["email"] == "anna@alpha.ru"
    assert card["role"] == "employee"

    assert admin.email in await people_emails(employee)

    r = await employee.patch("/account", json={"telegram": None})
    assert r.status_code == 204, r.text
    assert (await admin.get(f"/people/{member}")).json()["telegram"] is None


async def test_profile_name_is_mandatory_and_input_limited(kronto: Kronto) -> None:
    """ТЗ §4: имя и фамилия обязательны; длина полей ограничена; почта
    меняется не здесь (ТЗ §3 — отдельной процедурой)."""
    person = await register(kronto, "ivan@alpha.ru")
    for bad in (
        {"first_name": ""},
        {"last_name": ""},
        {"first_name": "И" * 101},
        {"patronymic": "О" * 101},
        {"phone": "1" * 33},
        {"telegram": "t" * 65},
        {"email": "other@alpha.ru"},
    ):
        r = await person.patch("/account", json=bad)
        assert r.status_code == 422, (bad, r.text)

    # допущение: null у обязательного поля и имя из одних пробелов — либо
    # отказ 4xx, либо имя не становится пустым.
    for blank in ({"first_name": None}, {"last_name": None}, {"first_name": "   "}):
        r = await person.patch("/account", json=blank)
        assert r.status_code == 204 or 400 <= r.status_code < 500, r.text
        me = await person.me()
        assert (me["first_name"] or "").strip(), me
        assert (me["last_name"] or "").strip(), me

    r = await person.patch("/account", json={"first_name": "Пётр"})
    assert r.status_code == 204, r.text
    me = await person.me()
    assert me["first_name"] == "Пётр"
    assert me["email"] == "ivan@alpha.ru"


async def test_people_directory_isolated_between_companies(kronto: Kronto) -> None:
    """ТЗ §4: профиль видят только коллеги по компании; вне компании — никто,
    и чужой админ не правит должность."""
    alpha = await new_company(kronto, "alpha")
    employee = await hire(kronto, alpha, "ivan@alpha.ru")
    beta = await new_company(kronto, "beta")
    member = await employee.member_id()

    assert await people_emails(beta) == {beta.email}
    assert beta.email not in await people_emails(employee)

    r = await beta.get(f"/people/{member}")
    assert r.status_code in FORBIDDEN, r.text
    assert "ivan@alpha.ru" not in r.text
    r = await beta.patch(f"/people/{member}", json={"position": "Шпион"})
    assert r.status_code in FORBIDDEN, r.text
    assert (await alpha.get(f"/people/{member}")).json()["position"] is None

    r = await employee.get(f"/people/{await beta.member_id()}")
    assert r.status_code in FORBIDDEN, r.text


async def test_account_without_company_sees_no_company_data(kronto: Kronto) -> None:
    """ТЗ §2: человек без компании видит профиль и настройки, но ничего из
    компаний: ни людей, ни отделов, ни админки."""
    await new_company(kronto, "alpha")
    person = await register(kronto, "ivan@alpha.ru")
    me = await person.me()
    assert me["company"] is None
    assert me["companies"] == []

    for path in (
        "/people",
        "/departments",
        "/users",
        "/invites",
        "/company",
        "/analytics",
        "/audit",
        "/glossary",
    ):
        r = await person.get(path)
        assert r.status_code in NO_ACCESS, (path, r.status_code, r.text)

    # Разбор 06.10: «@ivan» короче 5 знаков — правило самого Telegram.
    r = await person.patch("/account", json={"telegram": "@ivan_p"})
    assert r.status_code == 204, r.text
    r = await person.get("/account/company-requests")
    assert r.status_code == 200, r.text


async def test_position_and_department_self_service_and_admin(kronto: Kronto) -> None:
    """ТЗ §4, §7: должность и отдел заполняет сам человек, админ может поправить;
    чужую карточку сотрудник не правит."""
    admin = await new_company(kronto, "alpha")
    r = await admin.post("/departments", json={"name": "Бухгалтерия"})
    assert r.status_code == 201, r.text
    department = r.json()
    assert department["members"] == 0
    employee = await hire(kronto, admin, "ivan@alpha.ru")
    member = await employee.member_id()

    r = await employee.patch(
        f"/people/{member}",
        json={"position": "Бухгалтер", "department_id": department["id"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["position"] == "Бухгалтер"
    assert r.json()["department"]["id"] == department["id"]
    me = await employee.me()
    assert me["company"]["position"] == "Бухгалтер"
    assert me["company"]["department"]["id"] == department["id"]
    counts = {d["id"]: d["members"] for d in (await admin.get("/departments")).json()}
    assert counts[department["id"]] == 1

    admin_member = await admin.member_id()
    r = await employee.patch(f"/people/{admin_member}", json={"position": "Стажёр"})
    assert r.status_code in FORBIDDEN, r.text
    assert (await admin.get(f"/people/{admin_member}")).json()["position"] != "Стажёр"

    r = await admin.patch(f"/people/{member}", json={"position": "Главный бухгалтер"})
    assert r.status_code == 200, r.text
    assert (await employee.get(f"/people/{member}")).json()[
        "position"
    ] == "Главный бухгалтер"

    r = await employee.patch(f"/people/{member}", json={"position": "Д" * 101})
    assert r.status_code == 422, r.text
    r = await employee.patch(f"/people/{member}", json={"department_id": None})
    assert r.status_code == 200, r.text
    assert r.json()["department"] is None


async def test_departments_managed_by_admin_only(kronto: Kronto) -> None:
    """ТЗ §7: отделы заводит админ, сотрудник только выбирает; удаление отдела
    людей не удаляет."""
    admin = await new_company(kronto, "alpha")
    r = await admin.post("/departments", json={"name": "Продажи"})
    assert r.status_code == 201, r.text
    dep_id = r.json()["id"]
    employee = await hire(kronto, admin, "ivan@alpha.ru")

    r = await employee.get("/departments")
    assert r.status_code == 200, r.text
    assert "Продажи" in {d["name"] for d in r.json()}
    await assert_all_forbidden(
        lambda: employee.post("/departments", json={"name": "Тайный отдел"}),
        lambda: employee.patch(f"/departments/{dep_id}", json={"name": "Переименован"}),
        lambda: employee.delete(f"/departments/{dep_id}"),
    )
    assert {d["name"] for d in (await admin.get("/departments")).json()} == {"Продажи"}

    for bad in ({"name": ""}, {"name": "   "}, {"name": "x" * 101}, {}):
        r = await admin.post("/departments", json=bad)
        assert r.status_code == 422, (bad, r.text)

    r = await admin.patch(f"/departments/{dep_id}", json={"name": "Отдел продаж"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Отдел продаж"

    member = await employee.member_id()
    r = await admin.patch(f"/people/{member}", json={"department_id": dep_id})
    assert r.status_code == 200, r.text
    r = await admin.delete(f"/departments/{dep_id}")
    assert r.status_code == 204, r.text
    assert (await admin.get(f"/people/{member}")).json()["department"] is None
    assert employee.email in await people_emails(admin)
    assert dep_id not in {d["id"] for d in (await admin.get("/departments")).json()}


async def test_foreign_department_not_usable(kronto: Kronto) -> None:
    """ТЗ §4, §7: отдел принадлежит компании — отдел чужой компании не выбрать,
    не переименовать и не удалить."""
    alpha = await new_company(kronto, "alpha")
    employee = await hire(kronto, alpha, "ivan@alpha.ru")
    beta = await new_company(kronto, "beta")
    r = await beta.post("/departments", json={"name": "Бета-отдел"})
    assert r.status_code == 201, r.text
    beta_dep = r.json()["id"]
    member = await employee.member_id()

    r = await employee.patch(f"/people/{member}", json={"department_id": beta_dep})
    assert_refused(r)
    assert (await alpha.get(f"/people/{member}")).json()["department"] is None

    r = await alpha.patch(f"/departments/{beta_dep}", json={"name": "Захвачен"})
    assert r.status_code in FORBIDDEN, r.text
    r = await alpha.delete(f"/departments/{beta_dep}")
    assert r.status_code in FORBIDDEN, r.text

    beta_deps = {d["id"]: d for d in (await beta.get("/departments")).json()}
    assert beta_deps[beta_dep]["name"] == "Бета-отдел"
    assert beta_deps[beta_dep]["members"] == 0
    assert beta_dep not in {d["id"] for d in (await alpha.get("/departments")).json()}


async def test_avatar_upload_signed_url_and_validation(kronto: Kronto) -> None:
    """ТЗ §4: фото профиля — видно коллегам по подписанной ссылке; формат —
    по содержимому, а не по имени файла; до 5 МБ; фото можно убрать."""
    admin = await new_company(kronto, "alpha")
    employee = await hire(kronto, admin, "ivan@alpha.ru")

    r = await employee.put(
        "/account/avatar", files={"file": ("snimok.txt", png(), "text/plain")}
    )
    assert r.status_code == 200, r.text
    path, params = split_url(r.json()["avatar_url"])
    me = await employee.me()
    assert me["avatar_url"]
    assert me["id"] in path

    guest = kronto.browser()
    r = await guest.get(path, params=params)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/webp")
    card = (await admin.get(f"/people/{me['company']['member_id']}")).json()
    assert card["avatar_url"]

    r = await guest.get(path, params={**params, "sig": "0" * 32})
    assert_refused(r)
    r = await admin.put(
        "/account/avatar", files={"file": ("me.png", png(64, 64), "image/png")}
    )
    assert r.status_code == 200, r.text
    admin_id = (await admin.me())["id"]
    r = await guest.get(path.replace(me["id"], admin_id), params=params)
    assert_refused(r)

    r = await employee.put(
        "/account/avatar",
        files={"file": ("photo.png", b"definitely not an image", "image/png")},
    )
    assert_refused(r)
    r = await employee.put(
        "/account/avatar",
        files={"file": ("big.png", png(1500, 1500, noise=True), "image/png")},
    )
    assert_refused(r)

    r = await employee.delete("/account/avatar")
    assert r.status_code == 204, r.text
    assert (await employee.me())["avatar_url"] is None


# === настройки компании


async def test_company_settings_read_and_rename(kronto: Kronto) -> None:
    """ТЗ §7: настройки компании в интерфейсе — название меняется, изменение
    попадает в журнал; тариф, места и код компании админ сам не меняет."""
    admin = await new_company(
        kronto, "alpha", name="ООО Альфа", seats=7, tariff="extended"
    )
    settings = await company(admin)
    assert settings["id"] == admin.tenant_id
    assert settings["name"] == "ООО Альфа"
    assert settings["company_code"] == "alpha"
    assert settings["tariff"] == "extended"
    assert settings["seats"] == 7
    assert settings["members"] == 1
    assert settings["not_found_mode"] == "general"
    # допущение: по умолчанию второй фактор — код на почту (ТЗ §3), галочка
    # «запомнить» разрешена, домены не ограничены, логотипа нет.
    assert settings["mfa_policy"] == "any"
    assert settings["allow_remember_device"] is True
    assert settings["email_domains"] == []
    assert settings["logo_url"] is None

    audit_before = len((await admin.get("/audit", params={"limit": 200})).json())
    r = await admin.patch("/company", json={"name": "ООО Альфа Плюс"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "ООО Альфа Плюс"
    me = await admin.me()
    assert me["company"]["name"] == "ООО Альфа Плюс"
    assert {c["company_name"] for c in me["companies"]} == {"ООО Альфа Плюс"}
    audit_after = len((await admin.get("/audit", params={"limit": 200})).json())
    assert audit_after > audit_before

    for bad in (
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 201},
        {"tariff": "enterprise"},
        {"seats": 100},
        {"company_code": "omega"},
        {"email_domains": [f"d{i}.ru" for i in range(11)]},
        {"mfa_policy": "sms"},
    ):
        r = await admin.patch("/company", json=bad)
        assert r.status_code == 422, (bad, r.text)
    settings = await company(admin)
    assert settings["name"] == "ООО Альфа Плюс"
    assert (settings["tariff"], settings["seats"], settings["company_code"]) == (
        "extended",
        7,
        "alpha",
    )


async def test_employee_cannot_change_company_settings(kronto: Kronto) -> None:
    """ТЗ §2, §7: настройки, логотип и тариф компании — только администратор."""
    admin = await new_company(kronto, "alpha", name="ООО Альфа")
    employee = await hire(kronto, admin, "ivan@alpha.ru")

    await assert_all_forbidden(
        lambda: employee.patch("/company", json={"name": "Взломано"}),
        lambda: employee.patch(
            "/company", json={"mfa_policy": "any", "email_domains": []}
        ),
        lambda: employee.put(
            "/company/logo", files={"file": ("logo.png", png(), "image/png")}
        ),
        lambda: employee.delete("/company/logo"),
        lambda: employee.post("/company/tariff-request", json={"tariff": "enterprise"}),
    )

    settings = await company(admin)
    assert settings["name"] == "ООО Альфа"
    assert settings["logo_url"] is None
    assert settings["tariff"] == "base"


async def test_not_found_mode_switch_changes_answers(kronto: Kronto) -> None:
    """ТЗ §7: режим «ответа нет» в настройках: общий ответ с пометкой или
    честный отказ — переключение действует на следующий вопрос."""
    admin = await new_company(kronto, "alpha", not_found_mode="general")
    employee = await hire(kronto, admin, "ivan@alpha.ru")
    # Разные вопросы — чтобы возможный кэш ответов не смешивался с режимом.
    # В компании нет документов, так что ответа в документах нет ни на один.

    r = await employee.post(
        "/faq/ask", json={"question": "Сколько длится испытательный срок?"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["origin"] == "general_knowledge"
    assert r.json()["sources"] == []
    assert r.json()["content"].startswith(GENERAL_OR_REFUSAL_PREFIX)

    r = await admin.patch("/company", json={"not_found_mode": "strict"})
    assert r.status_code == 200, r.text
    assert r.json()["not_found_mode"] == "strict"
    r = await employee.post(
        "/faq/ask", json={"question": "Как оформить больничный лист?"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["origin"] == "none"
    assert r.json()["sources"] == []
    assert r.json()["content"].startswith(GENERAL_OR_REFUSAL_PREFIX)

    r = await admin.patch("/company", json={"not_found_mode": "general"})
    assert r.status_code == 200, r.text
    r = await employee.post("/faq/ask", json={"question": "Когда выплачивают аванс?"})
    assert r.status_code == 200, r.text
    assert r.json()["origin"] == "general_knowledge"


async def test_mfa_policy_strong_applies_to_employees(kronto: Kronto) -> None:
    """ТЗ §3, §7: админ может потребовать приложение или ключ от всех
    сотрудников; администраторам это обязательно всегда."""
    admin = await new_company(kronto, "alpha")
    employee = await hire(kronto, admin, "ivan@alpha.ru")
    assert (await employee.me())["mfa"]["strong_required"] is False
    assert (await admin.me())["mfa"]["strong_required"] is True

    r = await admin.patch("/company", json={"mfa_policy": "strong"})
    assert r.status_code == 200, r.text
    assert r.json()["mfa_policy"] == "strong"
    assert (await employee.me())["mfa"]["strong_required"] is True
    r = await employee.get("/account/security")
    assert r.status_code == 200, r.text
    assert r.json()["strong_required"] is True

    r = await admin.patch("/company", json={"mfa_policy": "any"})
    assert r.status_code == 200, r.text
    assert (await employee.me())["mfa"]["strong_required"] is False
    assert (await admin.me())["mfa"]["strong_required"] is True


async def test_company_can_forbid_remember_device(kronto: Kronto) -> None:
    """ТЗ §3, §7: «запомнить это устройство» избавляет от второго фактора, но
    админ компании может запретить галочку."""
    admin = await new_company(kronto, "alpha")
    employee = await hire(kronto, admin, "ivan@alpha.ru")

    # Контроль: пока галочка разрешена, браузер запоминается.
    # допущение: доверенное устройство хранится в cookie браузера.
    trusted = await login(kronto, employee.email, PASSWORD, remember=True)
    body = await login_request(trusted, employee.email, PASSWORD, remember=True)
    assert body["status"] == "ok", body

    r = await admin.patch("/company", json={"allow_remember_device": False})
    assert r.status_code == 200, r.text
    assert r.json()["allow_remember_device"] is False

    fresh = await login(kronto, employee.email, PASSWORD, remember=True)
    body = await login_request(fresh, employee.email, PASSWORD, remember=True)
    assert body["status"] == "mfa_required", body


async def test_company_email_domains_restrict_joining(kronto: Kronto) -> None:
    """ТЗ §7: домены почты компании — по приглашению вступает только почта
    этих доменов (пусто — любая)."""
    admin = await new_company(kronto, "alpha")
    r = await admin.patch("/company", json={"email_domains": ["alpha.ru"]})
    assert r.status_code == 200, r.text
    assert r.json()["email_domains"] == ["alpha.ru"]
    created = await make_invite(admin, max_uses=10)  # без своего домена

    stranger = await register(kronto, "ivan@beta.ru")
    r = await accept(stranger, created["code"])
    assert_refused(r)
    assert (await stranger.me())["company"] is None
    assert await user_by_email(admin, stranger.email) is None

    insider = await register(kronto, "anna@alpha.ru", first="Анна")
    r = await accept(insider, created["code"])
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "joined"

    r = await admin.patch("/company", json={"email_domains": []})
    assert r.status_code == 200, r.text
    assert r.json()["email_domains"] == []


async def test_company_logo_upload_signed_url_and_delete(kronto: Kronto) -> None:
    """ТЗ §1, §7: логотип компании — в настройках; он в шапке и переключателе
    компаний; отдаётся по подписанной ссылке; только PNG/JPEG/WebP до 5 МБ."""
    admin = await new_company(kronto, "alpha")
    r = await admin.put(
        "/company/logo", files={"file": ("logo.png", png(), "image/png")}
    )
    assert r.status_code == 200, r.text
    path, params = split_url(r.json()["logo_url"])
    assert admin.tenant_id in path

    guest = kronto.browser()
    r = await guest.get(path, params=params)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/webp")
    me = await admin.me()
    assert me["company"]["logo_url"]
    assert all(
        c["logo_url"] for c in me["companies"] if c["tenant_id"] == admin.tenant_id
    )
    assert (await company(admin))["logo_url"]

    r = await guest.get(path, params={**params, "sig": "0" * 32})
    assert_refused(r)
    r = await guest.get(path.replace(admin.tenant_id, str(uuid.uuid4())), params=params)
    assert_refused(r)

    r = await admin.put(
        "/company/logo",
        files={"file": ("logo.png", "это не картинка".encode(), "image/png")},
    )
    assert_refused(r)
    r = await admin.put(
        "/company/logo",
        files={"file": ("big.png", png(1500, 1500, noise=True), "image/png")},
    )
    assert_refused(r)
    assert (await company(admin))["logo_url"]

    r = await admin.delete("/company/logo")
    assert r.status_code == 204, r.text
    assert (await company(admin))["logo_url"] is None
    assert (await admin.me())["company"]["logo_url"] is None


# === аналитика, журнал, расход


async def test_analytics_overview_is_anonymous_and_admin_only(kronto: Kronto) -> None:
    """ТЗ §6, §7: главная админки — аналитика без имён: вопросы, активные
    сотрудники; сотруднику она недоступна."""
    admin = await new_company(kronto, "alpha")
    employee = await hire(
        kronto, admin, "zagorskaya@alpha.ru", first="Анастасия", last="Загорская"
    )
    r = await employee.post("/faq/ask", json={"question": "Как оформить командировку?"})
    assert r.status_code == 200, r.text
    me = await employee.me()

    r = await admin.get("/analytics")
    assert r.status_code == 200, r.text
    stats = r.json()
    # допущение: обзор считается по живым данным, без ночной задачи.
    assert stats["questions"] >= 1
    assert stats["active_people"] >= 1
    assert stats["members"] >= 2
    text = r.text
    for secret in (
        "zagorskaya",
        "Анастасия",
        "Загорская",
        me["id"],
        me["company"]["member_id"],
    ):
        assert secret.lower() not in text.lower(), secret

    r = await employee.get("/analytics")
    assert r.status_code in FORBIDDEN, r.text
    for days, expected in ((6, 422), (91, 422), (7, 200), (90, 200)):
        r = await admin.get("/analytics", params={"days": days})
        assert r.status_code == expected, (days, r.text)


async def test_analytics_isolated_between_companies(kronto: Kronto) -> None:
    """ТЗ §7: аналитика — только своей компании."""
    alpha = await new_company(kronto, "alpha")
    employee = await hire(kronto, alpha, "ivan@alpha.ru")
    r = await employee.post("/faq/ask", json={"question": "Где взять пропуск?"})
    assert r.status_code == 200, r.text
    beta = await new_company(kronto, "beta")

    r = await beta.get("/analytics")
    assert r.status_code == 200, r.text
    beta_stats = r.json()
    assert beta_stats["questions"] == 0
    assert beta_stats["answered"] == 0
    assert beta_stats["general"] == 0
    assert beta_stats["members"] == (await company(beta))["members"]
    assert "Где взять пропуск" not in r.text
    alpha_stats = (await alpha.get("/analytics")).json()
    assert alpha_stats["questions"] >= 1


async def test_audit_log_admin_only_and_paginated(kronto: Kronto) -> None:
    """ТЗ §7: журнал действий — у админа, новые сверху, постранично;
    сотруднику недоступен."""
    admin = await new_company(kronto, "alpha")
    for name in ("Альфа-1", "Альфа-2"):
        r = await admin.patch("/company", json={"name": name})
        assert r.status_code == 200, r.text

    r = await admin.get("/audit")
    assert r.status_code == 200, r.text
    events = r.json()
    assert len(events) >= 2
    times = [ts(e["created_at"]) for e in events]
    assert times == sorted(times, reverse=True)

    r = await admin.get("/audit", params={"limit": 1})
    assert r.status_code == 200, r.text
    first_page = r.json()
    assert [e["id"] for e in first_page] == [events[0]["id"]]
    r = await admin.get(
        "/audit", params={"limit": 1, "before": first_page[-1]["created_at"]}
    )
    assert r.status_code == 200, r.text
    second_page = r.json()
    assert len(second_page) <= 1
    assert events[0]["id"] not in {e["id"] for e in second_page}
    older = [
        e for e in events if ts(e["created_at"]) < ts(first_page[-1]["created_at"])
    ]
    if older:
        # следующая страница начинается с самой новой из более старых записей
        assert [e["id"] for e in second_page] == [older[0]["id"]]

    for params in ({"limit": 0}, {"limit": 201}, {"before": "вчера"}):
        r = await admin.get("/audit", params=params)
        assert r.status_code == 422, (params, r.text)

    employee = await hire(kronto, admin, "ivan@alpha.ru")
    r = await employee.get("/audit")
    assert r.status_code in FORBIDDEN, r.text


async def test_audit_log_isolated_between_companies(kronto: Kronto) -> None:
    """ТЗ §7: журнал — только своей компании; чужой не запросить даже
    параметром."""
    alpha = await new_company(kronto, "alpha")
    r = await alpha.patch("/company", json={"name": "Альфа Секретная"})
    assert r.status_code == 200, r.text
    beta = await new_company(kronto, "beta")
    r = await beta.patch("/company", json={"name": "Бета Открытая"})
    assert r.status_code == 200, r.text

    alpha_events = (await alpha.get("/audit", params={"limit": 200})).json()
    r = await beta.get("/audit", params={"limit": 200})
    assert r.status_code == 200, r.text
    beta_events = r.json()
    assert alpha_events and beta_events
    assert not {e["id"] for e in alpha_events} & {e["id"] for e in beta_events}
    assert "Альфа Секретная" not in r.text
    assert alpha.tenant_id not in r.text

    r = await beta.get("/audit", params={"limit": 200, "tenant_id": alpha.tenant_id})
    if r.status_code == 200:
        assert not {e["id"] for e in r.json()} & {e["id"] for e in alpha_events}
    else:
        assert_refused(r)


async def test_usage_pool_admin_only(kronto: Kronto) -> None:
    """ТЗ §7, §10: тариф — места и расход; пул общий, смотрит его админ."""
    admin = await new_company(kronto, "alpha", seats=3)
    r = await admin.get("/usage")
    assert r.status_code == 200, r.text
    usage = r.json()
    assert usage["seats"] == 3
    # допущение: пул = места × кредиты на место; новая компания ничего не потратила.
    assert usage["pool"] == usage["seats"] * usage["credits_per_seat"]
    assert usage["used"] == 0
    assert usage["remaining"] == usage["pool"]
    assert usage["exhausted"] is False

    employee = await hire(kronto, admin, "ivan@alpha.ru")
    r = await employee.get("/usage")
    assert r.status_code in FORBIDDEN, r.text


async def test_tariff_change_request(kronto: Kronto) -> None:
    """ТЗ §7, §10: страница тарифа — тариф и кнопка «сменить тариф», которая
    пишет команде; сам тариф меняет команда, а не админ; тарифы лендинга ушли."""
    admin = await new_company(kronto, "alpha", seats=5, tariff="base")
    r = await admin.get("/connectors/tariff")
    assert r.status_code == 200, r.text
    assert r.json()["tariff"] == "base"
    assert "Базовый" in r.json()["title"]

    r = await admin.post(
        "/company/tariff-request",
        json={
            "tariff": "extended",
            "seats": 20,
            "comment": "Растём, нужно больше мест",
        },
    )
    assert r.status_code == 202, r.text
    settings = await company(admin)
    assert (settings["tariff"], settings["seats"]) == ("base", 5)

    # Разбор 06.10: заявок на смену тарифа — не больше 5 в сутки на компанию,
    # неверные тоже считаются (защита от потока сообщений команде): вместе
    # с верной выше — пять.
    for bad in (
        {"tariff": "pro"},
        {"tariff": "extended", "seats": 0},
        {"tariff": "extended", "comment": "x" * 1001},
        {"comment": "без тарифа"},
    ):
        r = await admin.post("/company/tariff-request", json=bad)
        assert r.status_code == 422, (bad, r.text)


# === коннекторы


async def test_connector_catalog_respects_tariff(kronto: Kronto) -> None:
    """ТЗ §5, §10: каталог источников; небазовые системы доступны только
    в «Корпоративном» тарифе."""
    base_admin = await new_company(kronto, "alpha", tariff="base")
    base_kinds = await connector_kinds(base_admin)
    for kind in base_kinds:
        assert kind["kind"] and kind["title"]
        assert kind["mode"] in ("organization", "per_user")
        assert kind["available"] is bool(kind["base"]), kind

    enterprise_admin = await new_company(kronto, "beta", tariff="enterprise")
    enterprise_kinds = await connector_kinds(enterprise_admin)
    assert all(k["available"] for k in enterprise_kinds), enterprise_kinds
    assert {k["kind"] for k in enterprise_kinds} == {k["kind"] for k in base_kinds}


async def test_connector_count_limited_by_tariff(kronto: Kronto) -> None:
    """ТЗ §5, §10: тариф ограничивает число подключений; сверх лимита —
    отказ."""
    admin = await new_company(kronto, "alpha", tariff="base")
    r = await admin.get("/connectors/tariff")
    assert r.status_code == 200, r.text
    allowance = r.json()
    assert allowance["connectors"] == 0
    limit = allowance["connector_limit"]
    if limit > 20:
        pytest.skip(
            f"лимит базового тарифа {limit} — проверка перебором слишком долгая"
        )
    spec = creatable(await connector_kinds(admin))

    for i in range(limit):
        await create_connector(admin, spec, f"Источник {i + 1}", variant=i + 1)
    r = await admin.post(
        "/connectors",
        json=connector_payload(spec, f"Источник {limit + 1}", variant=limit + 1),
    )
    assert_refused(r)

    assert (await admin.get("/connectors/tariff")).json()["connectors"] == limit
    assert len((await admin.get("/connectors")).json()) == limit


async def test_non_base_connector_refused_on_base_tariff(kronto: Kronto) -> None:
    """ТЗ §5, §10: небазовую систему на «Базовом» тарифе не подключить."""
    admin = await new_company(kronto, "alpha", tariff="base")
    kinds = await connector_kinds(admin)
    locked = [k for k in kinds if not k["available"] and k["modules"]]
    if not locked:
        pytest.skip("в каталоге нет систем вне «Базового» тарифа")
    r = await admin.post(
        "/connectors", json=connector_payload(locked[0], "Закрытый источник")
    )
    assert_refused(r)
    assert (await admin.get("/connectors/tariff")).json()["connectors"] == 0
    assert (await admin.get("/connectors")).json() == []


async def test_connectors_managed_by_admin_only(kronto: Kronto) -> None:
    """ТЗ §5: подключения заводит и меняет админ; сотрудник видит «Мои
    подключения» и «где ищет ассистент»; админ видит, сколько подключилось."""
    admin = await new_company(kronto, "alpha", tariff="enterprise")
    spec = creatable(await connector_kinds(admin), prefer_mode="per_user")
    connector = await create_connector(admin, spec, "Портал компании")
    if connector["mode"] == "per_user":
        assert connector["grants_active"] == 0
    employee = await hire(kronto, admin, "ivan@alpha.ru")
    cid = connector["id"]

    secret = {"credentials": {"token": "x" * 16}}
    await assert_all_forbidden(
        lambda: employee.post(
            "/connectors", json=connector_payload(spec, "Своё подключение", variant=1)
        ),
        lambda: employee.patch(f"/connectors/{cid}", json={"name": "Чужое"}),
        lambda: employee.put(f"/connectors/{cid}/credentials", json=secret),
        lambda: employee.post(f"/connectors/{cid}/sync"),
        lambda: employee.delete(f"/connectors/{cid}"),
    )

    r = await admin.get(f"/connectors/{cid}")
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Портал компании"
    assert r.json()["credentials_set_at"] is None
    assert (await admin.get("/connectors/tariff")).json()["connectors"] == 1

    r = await employee.get("/connectors/mine")
    assert r.status_code == 200, r.text
    if connector["mode"] == "per_user":
        mine = {c["id"]: c for c in r.json()}
        assert cid in mine
        assert mine[cid]["grant_status"] is None
    r = await employee.get("/sources/mine")
    assert r.status_code == 200, r.text
    assert set(r.json()) >= {"files", "connectors"}


async def test_connector_input_validation(kronto: Kronto) -> None:
    """ТЗ §5: проверка ввода при подключении источника — неизвестная система,
    модули, название, интервал синхронизации."""
    admin = await new_company(kronto, "alpha", tariff="enterprise")
    spec = creatable(await connector_kinds(admin))
    good = connector_payload(spec, "Проверка")

    for bad in (
        {**good, "modules": []},
        {**good, "name": ""},
        {**good, "name": "x" * 101},
        {**good, "sync_interval_minutes": 14},
        {**good, "sync_interval_minutes": 1441},
        {**good, "client_secret": "s3cr3t"},
        {k: v for k, v in good.items() if k != "kind"},
    ):
        r = await admin.post("/connectors", json=bad)
        assert r.status_code == 422, (bad, r.text)
    for bad in (
        {**good, "kind": "sharepoint-xyz"},
        {**good, "modules": ["no_such_module"]},
    ):
        r = await admin.post("/connectors", json=bad)
        assert_refused(r)
    required = [f["name"] for f in spec["config_fields"] if f["required"]]
    if required:
        r = await admin.post("/connectors", json={**good, "config": {}})
        assert_refused(r)
    assert (await admin.get("/connectors")).json() == []

    r = await admin.post("/connectors", json=good)
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == spec["kind"]


async def test_connectors_isolated_between_companies(kronto: Kronto) -> None:
    """ТЗ §5, §2: подключения компании не видны и не доступны другой компании —
    ни её админу, ни её сотрудникам."""
    alpha = await new_company(kronto, "alpha", tariff="enterprise")
    spec = creatable(await connector_kinds(alpha), prefer_mode="per_user")
    connector = await create_connector(alpha, spec, "Портал Альфы")
    cid = connector["id"]
    alpha_employee = await hire(kronto, alpha, "ivan@alpha.ru")
    beta = await new_company(kronto, "beta", tariff="enterprise")
    beta_employee = await hire(kronto, beta, "anna@beta.ru", first="Анна")

    secret = {"credentials": {"token": "x" * 16}}
    await assert_all_forbidden(
        lambda: beta.get(f"/connectors/{cid}"),
        lambda: beta.get(f"/connectors/{cid}/runs"),
        lambda: beta.patch(f"/connectors/{cid}", json={"name": "Захвачено"}),
        lambda: beta.put(f"/connectors/{cid}/credentials", json=secret),
        lambda: beta.post(f"/connectors/{cid}/sync"),
        lambda: beta.post(f"/connectors/{cid}/test"),
        lambda: beta.delete(f"/connectors/{cid}"),
        lambda: beta_employee.put(f"/connectors/{cid}/mine", json=secret),
    )

    r = await alpha.get(f"/connectors/{cid}")
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Портал Альфы"
    assert r.json()["credentials_set_at"] is None
    assert cid not in {c["id"] for c in (await beta.get("/connectors")).json()}
    assert (await beta.get("/connectors/tariff")).json()["connectors"] == 0
    assert cid not in {
        c["id"] for c in (await beta_employee.get("/connectors/mine")).json()
    }
    beta_sources = (await beta_employee.get("/sources/mine")).json()
    assert cid not in {c["id"] for c in beta_sources["connectors"]}

    if connector["mode"] == "per_user":
        mine = (await alpha_employee.get("/connectors/mine")).json()
        assert cid in {c["id"] for c in mine}


# === глоссарий, пробелы


async def test_glossary_crud_admin_only(kronto: Kronto) -> None:
    """ТЗ §7: глоссарий ведёт админ; сотрудник его не меняет; длина ввода
    ограничена."""
    admin = await new_company(kronto, "alpha")
    r = await admin.post(
        "/glossary",
        json={"term": "ДМС", "expansion": "добровольное медицинское страхование"},
    )
    assert r.status_code == 201, r.text
    term_id = r.json()["id"]
    r = await admin.patch(
        f"/glossary/{term_id}",
        json={"expansion": "добровольное медицинское страхование сотрудников"},
    )
    assert r.status_code == 200, r.text

    for bad in (
        {"term": "Д", "expansion": "долгое слово"},
        {"term": "Т" * 65, "expansion": "долгое слово"},
        {"term": "ОМС", "expansion": "о"},
        {"term": "ОМС", "expansion": "о" * 257},
        {"term": "ОМС"},
    ):
        r = await admin.post("/glossary", json=bad)
        assert r.status_code == 422, (bad, r.text)

    employee = await hire(kronto, admin, "ivan@alpha.ru")
    await assert_all_forbidden(
        lambda: employee.post(
            "/glossary", json={"term": "НДФЛ", "expansion": "налог на доходы"}
        ),
        lambda: employee.patch(f"/glossary/{term_id}", json={"expansion": "испорчено"}),
        lambda: employee.delete(f"/glossary/{term_id}"),
    )

    terms = {t["term"]: t for t in (await admin.get("/glossary")).json()}
    assert set(terms) == {"ДМС"}
    assert (
        terms["ДМС"]["expansion"] == "добровольное медицинское страхование сотрудников"
    )

    r = await admin.delete(f"/glossary/{term_id}")
    assert r.status_code == 204, r.text
    assert (await admin.get("/glossary")).json() == []


async def test_glossary_isolated_between_companies(kronto: Kronto) -> None:
    """ТЗ §7, §2: глоссарий одной компании не виден и не изменяем из другой."""
    alpha = await new_company(kronto, "alpha")
    r = await alpha.post(
        "/glossary", json={"term": "КЭДО", "expansion": "кадровый ЭДО"}
    )
    assert r.status_code == 201, r.text
    term_id = r.json()["id"]
    beta = await new_company(kronto, "beta")

    r = await beta.get("/glossary")
    assert r.status_code == 200, r.text
    assert term_id not in r.text and "КЭДО" not in r.text
    await assert_all_forbidden(
        lambda: beta.patch(f"/glossary/{term_id}", json={"expansion": "захвачено"}),
        lambda: beta.delete(f"/glossary/{term_id}"),
    )

    terms = {t["id"]: t for t in (await alpha.get("/glossary")).json()}
    assert terms[term_id]["expansion"] == "кадровый ЭДО"


async def test_gaps_report_admin_only(kronto: Kronto) -> None:
    """ТЗ §7: отчёт о пробелах в документах — у админа; сотруднику недоступен."""
    admin = await new_company(kronto, "alpha")
    r = await admin.get("/gaps")
    assert r.status_code == 200, r.text
    assert r.json()["clusters"] == []
    r = await admin.get("/gaps", params={"status": "new", "limit": 100})
    assert r.status_code == 200, r.text
    for params in ({"status": "done"}, {"limit": 0}, {"limit": 101}):
        r = await admin.get("/gaps", params=params)
        assert r.status_code == 422, (params, r.text)
    r = await admin.patch(f"/gaps/{uuid.uuid4()}", json={"status": "resolved"})
    assert r.status_code in FORBIDDEN, r.text
    r = await admin.patch(f"/gaps/{uuid.uuid4()}", json={"status": "closed"})
    assert r.status_code == 422, r.text

    employee = await hire(kronto, admin, "ivan@alpha.ru")
    r = await employee.get("/gaps")
    assert r.status_code in FORBIDDEN, r.text
    r = await employee.patch(f"/gaps/{uuid.uuid4()}", json={"status": "dismissed"})
    assert r.status_code in FORBIDDEN, r.text


# === заявка «Подключить компанию»


async def test_company_request_create_list_cancel(kronto: Kronto) -> None:
    """ТЗ §2, §10: человек без компании подаёт заявку «Подключить компанию»,
    видит её и может отменить; чужую заявку — не видит и не отменяет."""
    applicant = await register(kronto, "founder@romashka.ru", first="Ольга")
    r = await applicant.get("/account/company-requests")
    assert r.status_code == 200, r.text
    assert r.json() == []

    r = await applicant.post(
        "/account/company-requests",
        json={"company_name": "ООО Ромашка", "seats": 15, "comment": "Хотим пилот"},
    )
    assert r.status_code == 201, r.text
    request = r.json()
    assert request["status"] == "new"
    assert request["company_name"] == "ООО Ромашка"
    assert request["seats"] == 15
    assert request["decided_at"] is None

    for bad in (
        {"company_name": "Я"},
        {"company_name": "x" * 201},
        {"company_name": "ООО Лютик", "seats": 0},
        {"company_name": "ООО Лютик", "comment": "x" * 2001},
        {"company_name": "ООО Лютик", "tariff": "base"},
        {},
    ):
        r = await applicant.post("/account/company-requests", json=bad)
        assert r.status_code == 422, (bad, r.text)
    assert [
        q["id"] for q in (await applicant.get("/account/company-requests")).json()
    ] == [request["id"]]

    other = await register(kronto, "other@lutik.ru", first="Пётр")
    r = await other.get("/account/company-requests")
    assert r.status_code == 200, r.text
    assert request["id"] not in r.text
    r = await other.post(f"/account/company-requests/{request['id']}/cancel")
    assert_refused(r)
    mine = (await applicant.get("/account/company-requests")).json()
    assert mine[0]["status"] == "new"

    r = await applicant.post(f"/account/company-requests/{request['id']}/cancel")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"
    r = await applicant.post(f"/account/company-requests/{uuid.uuid4()}/cancel")
    assert_refused(r)

    guest = kronto.browser()
    r = await guest.get(f"{API}/account/company-requests")
    assert r.status_code in UNAUTHENTICATED, r.text
    r = await guest.post(
        f"{API}/account/company-requests", json={"company_name": "ООО Гость"}
    )
    assert r.status_code in UNAUTHENTICATED, r.text


async def test_approved_company_request_makes_applicant_admin(kronto: Kronto) -> None:
    """ТЗ §2, §9: заявку одобряет команда kronto — заявитель становится
    администратором новой компании."""
    applicant = await register(
        kronto, "founder@romashka.ru", first="Ольга", last="Ромашкина"
    )
    r = await applicant.post(
        "/account/company-requests", json={"company_name": "ООО Ромашка", "seats": 5}
    )
    assert r.status_code == 201, r.text
    request_id = r.json()["id"]

    staff_email = "artem@krontoai.ru"
    await register(kronto, staff_email, first="Артём", last="Команда")
    await kronto.make_staff(staff_email)
    secret = await kronto.enable_totp(staff_email)
    staff = Actor(
        staff_email,
        PASSWORD,
        await login(kronto, staff_email, PASSWORD, totp=secret),
        totp=secret,
    )
    r = await staff.get("/staff/requests")
    assert r.status_code == 200, r.text
    assert request_id in {q["id"] for q in r.json()}
    r = await staff.post(
        f"/staff/requests/{request_id}/approve", json={"tariff": "base", "seats": 5}
    )
    assert r.status_code == 200, r.text
    tenant_id = r.json()["id"]
    assert r.json()["name"] == "ООО Ромашка"

    me = await applicant.me()
    memberships = {c["tenant_id"]: c for c in me["companies"]}
    assert tenant_id in memberships
    assert memberships[tenant_id]["role"] == "admin"
    assert memberships[tenant_id]["status"] == "active"
    [request] = (await applicant.get("/account/company-requests")).json()
    assert request["status"] == "approved"
    assert request["decided_at"] is not None


# === изоляция и вход


async def test_admin_cannot_manage_people_of_other_company(kronto: Kronto) -> None:
    """ТЗ §2, §7: админ одной компании не трогает людей другой: не убирает,
    не блокирует, не меняет роль, не одобряет заявки."""
    alpha = await new_company(kronto, "alpha")
    employee = await hire(kronto, alpha, "ivan@alpha.ru")
    gated = await make_invite(alpha, requires_approval=True)
    applicant = await register(kronto, "anna@alpha.ru", first="Анна")
    r = await accept(applicant, gated["code"])
    assert r.status_code == 200 and r.json()["outcome"] == "pending", r.text
    employee_uid = await user_id(alpha, employee.email)
    pending_uid = await user_id(alpha, applicant.email)
    beta = await new_company(kronto, "beta")

    await assert_all_forbidden(
        lambda: beta.delete(f"/users/{employee_uid}"),
        lambda: beta.patch(f"/users/{employee_uid}", json={"role": "admin"}),
        lambda: beta.patch(f"/users/{employee_uid}", json={"blocked": True}),
        lambda: beta.post(f"/users/{pending_uid}/approve"),
        lambda: beta.post(f"/users/{pending_uid}/reject"),
    )

    rows = {u["email"]: u for u in await users(alpha)}
    assert rows[employee.email]["status"] == "active"
    assert rows[employee.email]["role"] == "employee"
    assert rows[applicant.email]["status"] == "pending"
    assert (await employee.get("/people")).status_code == 200
    assert {u["email"] for u in await users(beta)} == {beta.email}


async def test_admin_cannot_touch_invites_of_other_company(kronto: Kronto) -> None:
    """ТЗ §2, §7: приглашения компании не видны и не отзываются из другой."""
    alpha = await new_company(kronto, "alpha", name="ООО Альфа")
    created = await make_invite(alpha)
    invite_id = created["invite"]["id"]
    beta = await new_company(kronto, "beta")

    r = await beta.delete(f"/invites/{invite_id}")
    assert r.status_code in FORBIDDEN, r.text
    r = await beta.get("/invites")
    assert r.status_code == 200, r.text
    assert invite_id not in r.text

    assert (await invite_state(alpha, invite_id))["status"] == "active"
    r = await kronto.browser().post(
        f"{API}/invites/preview", json={"secret": created["code"]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["company_name"] == "ООО Альфа"


async def test_company_endpoints_require_login(kronto: Kronto) -> None:
    """ТЗ §3: без входа ни людей, ни настроек, ни админки компании; вступить
    и переключиться тоже нельзя; поддельный токен не принимается."""
    created = await kronto.create_company(
        code="alpha",
        name="ООО Альфа",
        admin_email="admin@alpha.ru",
        admin_password=ADMIN_TEMP_PASSWORD,
    )
    tenant = created["id"]
    guest = kronto.browser()
    calls: list[tuple[str, str, dict[str, Any] | None]] = [
        ("GET", "/users", None),
        ("GET", "/invites", None),
        ("POST", "/invites", {}),
        ("POST", "/invites/accept", {"secret": "ABCD-EFGH"}),
        ("GET", "/company", None),
        ("PATCH", "/company", {"name": "Взлом"}),
        ("GET", "/people", None),
        ("GET", "/departments", None),
        ("GET", "/analytics", None),
        ("GET", "/audit", None),
        ("GET", "/usage", None),
        ("GET", "/connectors", None),
        ("GET", "/connectors/kinds", None),
        ("GET", "/connectors/tariff", None),
        ("GET", "/glossary", None),
        ("GET", "/gaps", None),
        ("GET", "/account/company-requests", None),
        ("POST", "/auth/switch-company", {"tenant_id": tenant}),
        ("POST", "/account/leave", {"tenant_id": tenant}),
    ]
    for method, path, body in calls:
        r = await guest.request(method, API + path, json=body)
        assert r.status_code in UNAUTHENTICATED, (method, path, r.status_code, r.text)

    guest.headers["Authorization"] = "Bearer not-a-real-token"
    for path in ("/people", "/company", "/auth/me"):
        r = await guest.get(API + path)
        assert r.status_code in UNAUTHENTICATED, (path, r.status_code, r.text)
