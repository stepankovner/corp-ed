"""Чёрный ящик: панель команды kronto, уведомления, первые шаги, поддержка
и публичный сайт (ТЗ §1, §2, §8, §9, §10, §11).

Тесты написаны только по ТЗ и схеме API, без чтения кода продукта. Где ТЗ
молчит о детали (точный код ошибки, текст письма), проверяется то, что
верно при любом разумном решении, и стоит пометка «# допущение: …».

Все администраторы и члены команды входят с приложением-аутентификатором:
ТЗ §3 — «администраторам компаний и команде kronto — обязательно
приложение или ключ доступа».
"""

from __future__ import annotations

import datetime as dt
import html as html_lib
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from tests.blackbox.conftest import API, Kronto

DOMAIN = "bb-kronto.ru"
STAFF_EMAIL = f"artem@{DOMAIN}"

CODE_RE = re.compile(r"(?<![\d#])(\d{6})(?!\d)")
URL_RE = re.compile(r"https?://[^\s\"'<>]+")
TOKEN_RE = re.compile(r"[A-Za-z0-9_\-.~%]{16,128}")

# Отказ: без входа — 401/403; чужая роль — 401/403 или 404 (ручку можно
# прятать). ТЗ точный код не задаёт.
NO_LOGIN = (401, 403)
REFUSED = (401, 403, 404)


# --------------------------------------------------------------------------
# Вспомогательное: учётки, вход, компании (только по схеме API)
# --------------------------------------------------------------------------


def new_password() -> str:
    return f"Qz{secrets.token_hex(8)}-Wv7!"


def rnd() -> str:
    return str(uuid.uuid4())


def is_4xx(response: httpx.Response) -> bool:
    return 400 <= response.status_code < 500


@dataclass
class Person:
    email: str
    password: str
    totp: str | None = None
    client: httpx.AsyncClient | None = None

    @property
    def http(self) -> httpx.AsyncClient:
        assert self.client is not None, f"{self.email}: нет входа"
        return self.client


def set_bearer(client: httpx.AsyncClient, token: str) -> None:
    client.headers["Authorization"] = f"Bearer {token}"


def plain(letter: Any) -> str:
    """Тема, текст и HTML письма без тегов и стилей."""
    raw = getattr(letter, "html", None) or ""
    raw = re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", " ", raw)
    raw = html_lib.unescape(re.sub(r"<[^>]+>", " ", raw))
    return f"{letter.subject or ''}\n{letter.text or ''}\n{raw}"


def six_digit_code(letters: list[Any]) -> str | None:
    """Код из 6 цифр из самого свежего письма, где он есть."""
    for letter in reversed(letters):
        match = CODE_RE.search(plain(letter))
        if match:
            return match.group(1)
    return None


def link_tokens(letters: list[Any]) -> list[str]:
    """Похожие на токен части ссылок письма: параметры, фрагмент, хвост пути."""
    found: list[str] = []
    for letter in letters:
        blob = f"{letter.text or ''}\n{html_lib.unescape(letter.html or '')}"
        for url in URL_RE.findall(blob):
            parsed = urlparse(url.rstrip(".,;:!?)»"))
            parts: list[str] = []
            fragment_query = parsed.fragment.partition("?")[2] or parsed.fragment
            for query in (parsed.query, fragment_query):
                for values in parse_qs(query).values():
                    parts.extend(values)
            parts.append(parsed.fragment.rstrip("/").rpartition("/")[2])
            parts.append(parsed.path.rstrip("/").rpartition("/")[2])
            for part in parts:
                if TOKEN_RE.fullmatch(part) and part not in found:
                    found.append(part)
    return found


def sse_events(text: str) -> list[dict[str, Any]]:
    """События потока «data: <json>»."""
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def answered(response: httpx.Response) -> bool:
    """Чат выдал ответ: поток дошёл до события done."""
    if response.status_code != 200:
        return False
    return any(event.get("type") == "done" for event in sse_events(response.text))


async def register(
    kronto: Kronto, email: str, *, first: str = "Анна", last: str = "Тестова"
) -> Person:
    """Регистрация (ТЗ §2) и подтверждение почты кодом (ТЗ §3) — сразу вход."""
    person = Person(email=email, password=new_password())
    browser = kronto.browser()
    response = await browser.post(
        f"{API}/auth/register",
        json={
            "first_name": first,
            "last_name": last,
            "email": email,
            "password": person.password,
            "consent": True,
        },
    )
    assert response.status_code == 202, response.text
    code = six_digit_code(await kronto.inbox(email))
    assert code, f"{email}: нет письма с кодом подтверждения"
    response = await browser.post(
        f"{API}/auth/verify-email", json={"email": email, "code": code}
    )
    assert response.status_code == 200, response.text
    set_bearer(browser, response.json()["access_token"])
    person.client = browser
    return person


async def start_login(
    kronto: Kronto,
    person: Person,
    *,
    password: str | None = None,
    remember: bool = True,
) -> tuple[httpx.AsyncClient, httpx.Response]:
    browser = kronto.browser()
    response = await browser.post(
        f"{API}/auth/login",
        json={
            "email": person.email,
            "password": password or person.password,
            "remember": remember,
        },
    )
    return browser, response


async def email_login_code(
    kronto: Kronto,
    email: str,
    baseline: int,
    browser: httpx.AsyncClient,
    mfa_token: str,
) -> str | None:
    """Код входа из писем, пришедших после baseline; нет — один раз «прислать ещё»."""
    code = six_digit_code((await kronto.inbox(email))[baseline:])
    if code is None:
        await browser.post(f"{API}/auth/mfa/resend", json={"token": mfa_token})
        code = six_digit_code((await kronto.inbox(email))[baseline:])
    return code


async def finish_temporary_password(browser: httpx.AsyncClient, person: Person) -> None:
    """Временный пароль (учётка от create_company) — сразу сменить."""
    me = await browser.get(f"{API}/auth/me")
    assert me.status_code == 200, me.text
    if me.json().get("must_change_password"):
        new = new_password()
        response = await browser.post(
            f"{API}/auth/change-password",
            json={"current_password": person.password, "new_password": new},
        )
        assert response.status_code == 200, response.text
        person.password = new
        set_bearer(browser, response.json()["access_token"])


async def login(
    kronto: Kronto, person: Person, *, method: str | None = None
) -> httpx.AsyncClient:
    """Полный вход в новом браузере: пароль + второй фактор (ТЗ §3)."""
    method = method or ("totp" if person.totp else "email")
    baseline = len(await kronto.inbox(person.email)) if method == "email" else 0
    browser, response = await start_login(kronto, person)
    assert response.status_code == 200, response.text
    body = response.json()
    if body["status"] == "ok":
        token = body["access_token"]
    else:
        mfa = body["mfa"]
        if method == "totp":
            assert person.totp
            code = kronto.totp(person.totp)
        else:
            code = await email_login_code(
                kronto, person.email, baseline, browser, mfa["token"]
            )
            assert code, f"{person.email}: нет письма с кодом входа"
        verify = await browser.post(
            f"{API}/auth/mfa/verify",
            json={"token": mfa["token"], "method": method, "code": code},
        )
        assert verify.status_code == 200, verify.text
        token = verify.json()["access_token"]
    set_bearer(browser, token)
    await finish_temporary_password(browser, person)
    person.client = browser
    return browser


async def try_login(kronto: Kronto, person: Person) -> httpx.AsyncClient | None:
    """Вход с приложением; любой отказ по дороге — None."""
    assert person.totp
    browser, response = await start_login(kronto, person)
    if response.status_code != 200:
        return None
    body = response.json()
    if body["status"] == "ok":
        set_bearer(browser, body["access_token"])
        return browser
    verify = await browser.post(
        f"{API}/auth/mfa/verify",
        json={
            "token": body["mfa"]["token"],
            "method": "totp",
            "code": kronto.totp(person.totp),
        },
    )
    if verify.status_code != 200:
        return None
    set_bearer(browser, verify.json()["access_token"])
    return browser


async def me_of(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get(f"{API}/auth/me")
    assert response.status_code == 200, response.text
    return response.json()


async def use_company(person: Person, tenant_id: str) -> None:
    """Выбрать компанию в сеансе, если выбрана не она (ТЗ §2, переключатель)."""
    me = await me_of(person.http)
    current = me.get("company") or {}
    if current.get("tenant_id") != tenant_id:
        response = await person.http.post(
            f"{API}/auth/switch-company", json={"tenant_id": tenant_id}
        )
        assert response.status_code == 200, response.text
        set_bearer(person.http, response.json()["access_token"])


async def new_company(
    kronto: Kronto,
    code: str,
    *,
    name: str | None = None,
    seats: int = 10,
    tariff: str = "base",
    totp: bool = True,
    login_now: bool = True,
) -> tuple[Person, str]:
    """Компания и её администратор (как после созвона, `cli create-tenant`)."""
    email = f"admin.{code}@{DOMAIN}"
    person = Person(email=email, password=new_password())
    company = await kronto.create_company(
        code=code,
        name=name or f"Компания {code.upper()}",
        admin_email=email,
        admin_password=person.password,
        seats=seats,
        tariff=tariff,
    )
    tenant_id = str(company["id"])
    if totp:
        person.totp = await kronto.enable_totp(email)
    if login_now:
        await login(kronto, person)
        await use_company(person, tenant_id)
    return person, tenant_id


async def new_staff(kronto: Kronto) -> Person:
    """Член команды kronto: своя учётка, приложение-аутентификатор, вход заново."""
    person = await register(kronto, STAFF_EMAIL, first="Артём", last="Командный")
    await kronto.make_staff(STAFF_EMAIL)
    person.totp = await kronto.enable_totp(STAFF_EMAIL)
    await login(kronto, person)
    return person


async def new_employee(
    kronto: Kronto,
    admin: Person,
    local: str,
    *,
    first: str = "Пётр",
    last: str = "Сотрудников",
    approval: bool = False,
) -> tuple[Person, str]:
    """Сотрудник: регистрация и вступление по коду приглашения (ТЗ §2)."""
    response = await admin.http.post(
        f"{API}/invites", json={"requires_approval": approval}
    )
    assert response.status_code == 201, response.text
    secret = response.json()["code"]
    person = await register(kronto, f"{local}@{DOMAIN}", first=first, last=last)
    response = await person.http.post(f"{API}/invites/accept", json={"secret": secret})
    assert response.status_code == 200, response.text
    body = response.json()
    if body.get("session"):
        set_bearer(person.http, body["session"]["access_token"])
    return person, body["outcome"]


async def add_document(
    kronto: Kronto, admin: Person, title: str, content: str
) -> dict[str, Any]:
    response = await admin.http.post(
        f"{API}/materials", json={"title": title, "content": content}
    )
    assert response.status_code == 201, response.text
    await kronto.run_background()
    return response.json()


async def ask(client: httpx.AsyncClient, question: str) -> httpx.Response:
    """Вопрос в новом диалоге чата (ТЗ §6): поток событий."""
    return await client.post(f"{API}/conversations", json={"question": question})


async def create_request(
    person: Person, name: str, *, seats: int | None = None, comment: str | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"company_name": name}
    if seats is not None:
        body["seats"] = seats
    if comment is not None:
        body["comment"] = comment
    response = await person.http.post(f"{API}/account/company-requests", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def my_requests(person: Person) -> dict[str, dict[str, Any]]:
    response = await person.http.get(f"{API}/account/company-requests")
    assert response.status_code == 200, response.text
    return {item["id"]: item for item in response.json()}


async def staff_requests(
    staff: Person, status: str = "new"
) -> dict[str, dict[str, Any]]:
    response = await staff.http.get(f"{API}/staff/requests", params={"status": status})
    assert response.status_code == 200, response.text
    return {item["id"]: item for item in response.json()}


async def staff_companies(staff: Person) -> dict[str, dict[str, Any]]:
    response = await staff.http.get(f"{API}/staff/companies")
    assert response.status_code == 200, response.text
    return {item["id"]: item for item in response.json()}


async def staff_company(staff: Person, tenant_id: str) -> dict[str, Any]:
    response = await staff.http.get(f"{API}/staff/companies/{tenant_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def staff_patch(staff: Person, tenant_id: str, **changes: Any) -> httpx.Response:
    # допущение: смысл поля confirm схема не объясняет; шлём подтверждение
    # всегда, когда изменение должно пройти.
    return await staff.http.patch(
        f"{API}/staff/companies/{tenant_id}", json={**changes, "confirm": True}
    )


async def company_settings(admin: Person) -> dict[str, Any]:
    response = await admin.http.get(f"{API}/company")
    assert response.status_code == 200, response.text
    return response.json()


async def bell(person: Person) -> dict[str, Any]:
    response = await person.http.get(f"{API}/notifications")
    assert response.status_code == 200, response.text
    return response.json()


async def set_notify(person: Person, **flags: bool) -> dict[str, Any]:
    response = await person.http.put(f"{API}/notifications/settings", json=flags)
    assert response.status_code == 200, response.text
    return response.json()


async def onboarding(person: Person) -> dict[str, Any]:
    response = await person.http.get(f"{API}/onboarding")
    assert response.status_code == 200, response.text
    return response.json()


async def support_mine(person: Person) -> dict[str, dict[str, Any]]:
    response = await person.http.get(f"{API}/support/mine")
    assert response.status_code == 200, response.text
    return {item["id"]: item for item in response.json()}


def staff_calls(
    tenant_id: str, request_id: str, account_id: str, support_id: str, lead_id: str
) -> list[tuple[str, str, dict[str, Any] | None]]:
    """Все ручки панели команды, с корректными телами (чтобы 422 не выдал
    себя за отказ)."""
    return [
        ("GET", "/staff/overview", None),
        ("GET", "/staff/companies", None),
        ("GET", f"/staff/companies/{tenant_id}", None),
        (
            "PATCH",
            f"/staff/companies/{tenant_id}",
            {"seats": 99, "tariff": "enterprise", "confirm": True},
        ),
        ("GET", "/staff/requests?status=all", None),
        (
            "POST",
            f"/staff/requests/{request_id}/approve",
            {"tariff": "enterprise", "seats": 50},
        ),
        ("POST", f"/staff/requests/{request_id}/reject", None),
        ("GET", "/staff/spend?days=30", None),
        ("GET", "/staff/people?q=admin", None),
        ("GET", f"/staff/people/{account_id}", None),
        ("POST", f"/staff/people/{account_id}/password-reset", None),
        ("GET", "/staff/support", None),
        ("PATCH", f"/staff/support/{support_id}", {"status": "closed"}),
        ("GET", "/staff/leads", None),
        ("PATCH", f"/staff/leads/{lead_id}", {"status": "contacted"}),
    ]


async def assert_all_refused(
    client: httpx.AsyncClient,
    calls: list[tuple[str, str, dict[str, Any] | None]],
    allowed: tuple[int, ...],
    who: str,
) -> None:
    for method, path, body in calls:
        response = await client.request(method, f"{API}{path}", json=body)
        assert response.status_code in allowed, (
            f"{who}: {method} {path} → {response.status_code} {response.text[:300]}"
        )


# --------------------------------------------------------------------------
# §9. Доступ к панели команды
# --------------------------------------------------------------------------


async def test_staff_panel_refuses_anonymous_and_forged_token(kronto: Kronto) -> None:
    """ТЗ §9: панель команды — только для команды kronto. Без входа и с
    поддельным токеном ни одна ручка /staff/* не отвечает."""
    anonymous = kronto.browser()
    forged = kronto.browser()
    set_bearer(forged, "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlLWZha2U")
    calls = staff_calls(rnd(), rnd(), rnd(), rnd(), rnd())
    # допущение: без входа — 401 (или 403); точный код ТЗ не задаёт
    await assert_all_refused(anonymous, calls, NO_LOGIN, "без входа")
    await assert_all_refused(forged, calls, NO_LOGIN, "поддельный токен")


async def test_staff_panel_refuses_company_admin(kronto: Kronto) -> None:
    """ТЗ §9, §2: администратор компании — не команда kronto. Панель ему
    недоступна: свой тариф и места через неё не поменять, чужую заявку не
    одобрить, сброс пароля чужому человеку не отправить."""
    admin, tenant = await new_company(kronto, "alfa")
    applicant = await register(kronto, f"applicant@{DOMAIN}")
    request = await create_request(applicant, "ООО Заявка", seats=5)
    applicant_id = (await me_of(applicant.http))["id"]
    support = await admin.http.post(
        f"{API}/support",
        json={"topic": "other", "message": "Проверка обращения в поддержку"},
    )
    assert support.status_code == 201, support.text
    letters_before = len(await kronto.inbox(applicant.email))

    me = await me_of(admin.http)
    assert me.get("staff", False) is False

    calls = staff_calls(
        tenant, request["id"], applicant_id, support.json()["id"], rnd()
    )
    await assert_all_refused(admin.http, calls, REFUSED, "администратор компании")

    settings = await company_settings(admin)
    assert settings["seats"] == 10
    assert settings["tariff"] == "base"
    assert (await my_requests(applicant))[request["id"]]["status"] == "new"
    assert (await me_of(applicant.http))["companies"] == []
    assert len(await kronto.inbox(applicant.email)) == letters_before
    assert (await support_mine(admin))[support.json()["id"]]["status"] == "new"


async def test_staff_panel_refuses_employee(kronto: Kronto) -> None:
    """ТЗ §9, §2: сотрудник компании в панель команды не попадает и ничего
    в ней не меняет."""
    admin, tenant = await new_company(kronto, "alfa")
    employee, outcome = await new_employee(kronto, admin, "petr")
    assert outcome == "joined"
    employee_id = (await me_of(employee.http))["id"]
    support = await employee.http.post(
        f"{API}/support",
        json={"topic": "answers", "message": "Ассистент не нашёл ответ про отпуск"},
    )
    assert support.status_code == 201, support.text
    letters_before = len(await kronto.inbox(employee.email))

    calls = staff_calls(tenant, rnd(), employee_id, support.json()["id"], rnd())
    await assert_all_refused(employee.http, calls, REFUSED, "сотрудник")

    settings = await company_settings(admin)
    assert (settings["seats"], settings["tariff"]) == (10, "base")
    assert (await support_mine(employee))[support.json()["id"]]["status"] == "new"
    assert len(await kronto.inbox(employee.email)) == letters_before


async def test_staff_panel_requires_app_or_passkey(kronto: Kronto) -> None:
    """ТЗ §9, §3: в панель команды — только со входом через приложение или
    ключ доступа. Код на почту панель не открывает; с приложением — открывает."""
    staff = await register(kronto, STAFF_EMAIL, first="Артём", last="Командный")
    await kronto.make_staff(STAFF_EMAIL)
    panel = (
        "/staff/overview",
        "/staff/companies",
        "/staff/requests",
        "/staff/spend",
        "/staff/support",
    )

    # Сеанс после подтверждения почты кодом — не надёжный второй фактор.
    response = await staff.http.get(f"{API}/staff/overview")
    assert response.status_code in REFUSED, response.text

    # Вход заново с кодом на почту: либо сам вход не пускают, либо панель закрыта.
    baseline = len(await kronto.inbox(STAFF_EMAIL))
    browser, response = await start_login(kronto, staff, remember=False)
    opened = False
    if response.status_code != 200:
        assert is_4xx(response), response.text
    elif response.json()["status"] == "ok":
        set_bearer(browser, response.json()["access_token"])
        opened = True
    elif "email" in response.json()["mfa"]["methods"]:
        mfa_token = response.json()["mfa"]["token"]
        code = await email_login_code(kronto, STAFF_EMAIL, baseline, browser, mfa_token)
        if code is not None:
            verify = await browser.post(
                f"{API}/auth/mfa/verify",
                json={"token": mfa_token, "method": "email", "code": code},
            )
            if verify.status_code == 200:
                set_bearer(browser, verify.json()["access_token"])
                opened = True
    if opened:
        for path in panel:
            response = await browser.get(f"{API}{path}")
            assert response.status_code in REFUSED, (
                f"{path}: вход по коду на почту открыл панель"
            )

    # С приложением-аутентификатором — панель открыта.
    staff.totp = await kronto.enable_totp(STAFF_EMAIL)
    client = await login(kronto, staff)
    assert (await me_of(client)).get("staff") is True
    for path in panel:
        response = await client.get(f"{API}{path}")
        assert response.status_code == 200, f"{path}: {response.text}"


async def test_applicant_cannot_decide_own_request(kronto: Kronto) -> None:
    """ТЗ §2, §9: заявку «Подключить компанию» одобряет только команда
    kronto; сам заявитель одобрить или отклонить её не может."""
    applicant = await register(kronto, f"applicant@{DOMAIN}")
    request = await create_request(applicant, "ООО Самоодобрение", seats=5)

    approve = await applicant.http.post(
        f"{API}/staff/requests/{request['id']}/approve",
        json={"tariff": "enterprise", "seats": 100},
    )
    assert approve.status_code in REFUSED, approve.text
    reject = await applicant.http.post(f"{API}/staff/requests/{request['id']}/reject")
    assert reject.status_code in REFUSED, reject.text

    assert (await my_requests(applicant))[request["id"]]["status"] == "new"
    assert (await me_of(applicant.http))["companies"] == []


async def test_staff_member_does_not_get_into_company_data(kronto: Kronto) -> None:
    """ТЗ §9, §6: панель команды — про заявки, тарифы и вход; членом
    компании команда от этого не становится: войти в компанию и читать её
    документы нельзя."""
    staff = await new_staff(kronto)
    admin, tenant = await new_company(kronto, "alfa")
    marker = "СЕКРЕТНЫЙДОКУМЕНТ3141"
    await add_document(
        kronto, admin, "Регламент Альфы", f"Внутренний регламент склада: {marker}."
    )

    switch = await staff.http.post(
        f"{API}/auth/switch-company", json={"tenant_id": tenant}
    )
    assert is_4xx(switch), switch.text
    me = await me_of(staff.http)
    assert all(item["tenant_id"] != tenant for item in me["companies"])
    assert (me.get("company") or {}).get("tenant_id") != tenant

    for path in ("/materials", "/conversations", "/people", "/users", "/company"):
        response = await staff.http.get(f"{API}{path}")
        if response.status_code == 200:
            assert marker not in response.text, path
            assert "Регламент Альфы" not in response.text, path
            assert admin.email not in response.text, path
        else:
            assert is_4xx(response), f"{path}: {response.status_code}"


# --------------------------------------------------------------------------
# §2, §9, §10. Заявка «Подключить компанию»
# --------------------------------------------------------------------------


async def test_company_request_approved_applicant_becomes_admin(kronto: Kronto) -> None:
    """ТЗ §2, §9, §10: человек без компании подаёт заявку «Подключить
    компанию» → команда одобряет её в панели (тариф, места, срок пилота) →
    компания есть, заявитель — её администратор."""
    staff = await new_staff(kronto)
    applicant = await register(
        kronto, f"maria@{DOMAIN}", first="Мария", last="Заявкина"
    )

    request = await create_request(
        applicant, "ООО Ромашка", seats=15, comment="Нужно на 15 человек"
    )
    assert request["status"] == "new"
    assert request["company_name"] == "ООО Ромашка"
    assert request["seats"] == 15
    assert request["decided_at"] is None
    assert request["id"] in await my_requests(applicant)

    queued = (await staff_requests(staff))[request["id"]]
    assert queued["applicant_email"] == applicant.email
    assert queued["company_name"] == "ООО Ромашка"
    assert queued["status"] == "new"
    assert queued["tenant_id"] is None

    pilot = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    approve = await staff.http.post(
        f"{API}/staff/requests/{request['id']}/approve",
        json={"tariff": "extended", "seats": 15, "pilot_until": pilot},
    )
    assert approve.status_code == 200, approve.text
    company = approve.json()
    assert company["name"] == "ООО Ромашка"
    assert company["tariff"] == "extended"
    assert company["seats"] == 15
    assert company["pilot_until"] == pilot
    assert company["is_active"] is True
    assert company["company_code"]
    # допущение: в списке admins — почта (или имя) администратора
    assert any(
        applicant.email in admin or "Мария" in admin for admin in company["admins"]
    )

    mine = (await my_requests(applicant))[request["id"]]
    assert mine["status"] == "approved"
    assert mine["decided_at"] is not None
    decided = (await staff_requests(staff, "all"))[request["id"]]
    assert decided["status"] == "approved"
    assert decided["tenant_id"] == company["id"]
    assert request["id"] not in await staff_requests(staff, "new")
    assert company["id"] in await staff_companies(staff)

    # Администратору — обязательно приложение (ТЗ §3): входит заново с ним.
    applicant.totp = await kronto.enable_totp(applicant.email)
    await login(kronto, applicant)
    me = await me_of(applicant.http)
    memberships = [
        item for item in me["companies"] if item["tenant_id"] == company["id"]
    ]
    assert len(memberships) == 1
    assert memberships[0]["role"] == "admin"
    assert memberships[0]["status"] == "active"
    await use_company(applicant, company["id"])
    assert (await me_of(applicant.http))["company"]["role"] == "admin"
    settings = await company_settings(applicant)
    assert (settings["name"], settings["tariff"], settings["seats"]) == (
        "ООО Ромашка",
        "extended",
        15,
    )


async def test_company_request_approval_letter(kronto: Kronto) -> None:
    """ТЗ §2, §9: одобрили заявку — заявителю уходит письмо (схема: «ему
    уходит письмо»)."""
    staff = await new_staff(kronto)
    applicant = await register(
        kronto, f"maria@{DOMAIN}", first="Мария", last="Заявкина"
    )
    request = await create_request(applicant, "ООО Ромашка", seats=12)
    before = len(await kronto.inbox(applicant.email))

    approve = await staff.http.post(
        f"{API}/staff/requests/{request['id']}/approve",
        json={"tariff": "base", "seats": 12},
    )
    assert approve.status_code == 200, approve.text

    letters = (await kronto.inbox(applicant.email))[before:]
    assert letters, "заявителю не пришло письмо об одобрении"
    # допущение: письмо называет компанию, по которой одобрена заявка
    assert any("Ромашка" in plain(letter) for letter in letters)


async def test_company_request_rejected(kronto: Kronto) -> None:
    """ТЗ §2, §9: команда отклоняет заявку — компания не создаётся, заявитель
    видит отказ, заявка уходит из очереди новых."""
    staff = await new_staff(kronto)
    applicant = await register(kronto, f"maria@{DOMAIN}")
    request = await create_request(applicant, "ООО Отказ", seats=3)

    reject = await staff.http.post(f"{API}/staff/requests/{request['id']}/reject")
    assert reject.status_code == 204, reject.text

    mine = (await my_requests(applicant))[request["id"]]
    assert mine["status"] == "rejected"
    assert mine["decided_at"] is not None
    assert request["id"] not in await staff_requests(staff, "new")
    decided = (await staff_requests(staff, "all"))[request["id"]]
    assert decided["status"] == "rejected"
    assert decided["tenant_id"] is None
    assert await staff_companies(staff) == {}
    assert (await me_of(applicant.http))["companies"] == []


async def test_company_request_rejection_letter(kronto: Kronto) -> None:
    """ТЗ §2, §9: о решении по заявке заявитель узнаёт письмом и при отказе."""
    staff = await new_staff(kronto)
    applicant = await register(kronto, f"maria@{DOMAIN}")
    request = await create_request(applicant, "ООО Отказ")
    before = len(await kronto.inbox(applicant.email))

    reject = await staff.http.post(f"{API}/staff/requests/{request['id']}/reject")
    assert reject.status_code == 204, reject.text

    # допущение: ТЗ говорит только «одобрить, отклонить»; письмо об отказе —
    # ожидание задачи («approve/reject with emails»), не прямая строка ТЗ.
    letters = (await kronto.inbox(applicant.email))[before:]
    assert letters, "заявителю не пришло письмо об отказе"


async def test_decided_request_cannot_be_decided_again(kronto: Kronto) -> None:
    """ТЗ §2, §9: одобренная заявка — одна компания: повторное одобрение,
    отказ после одобрения и отмена заявителем не проходят."""
    staff = await new_staff(kronto)
    applicant = await register(kronto, f"maria@{DOMAIN}")
    request = await create_request(applicant, "ООО Однажды", seats=4)

    first = await staff.http.post(
        f"{API}/staff/requests/{request['id']}/approve", json={"tariff": "base"}
    )
    assert first.status_code == 200, first.text

    again = await staff.http.post(
        f"{API}/staff/requests/{request['id']}/approve", json={"tariff": "base"}
    )
    # допущение: повтор — 4xx (скорее всего 409)
    assert is_4xx(again), again.text
    reject = await staff.http.post(f"{API}/staff/requests/{request['id']}/reject")
    assert is_4xx(reject), reject.text
    cancel = await applicant.http.post(
        f"{API}/account/company-requests/{request['id']}/cancel"
    )
    assert is_4xx(cancel), cancel.text

    assert (await my_requests(applicant))[request["id"]]["status"] == "approved"
    companies = await staff_companies(staff)
    assert list(companies) == [first.json()["id"]]


async def test_cancelled_request_cannot_be_approved(kronto: Kronto) -> None:
    """ТЗ §2, §9: заявитель отменил заявку — она уходит из очереди, команда
    её уже не одобрит и не отклонит."""
    staff = await new_staff(kronto)
    applicant = await register(kronto, f"maria@{DOMAIN}")
    request = await create_request(applicant, "ООО Передумали")

    cancel = await applicant.http.post(
        f"{API}/account/company-requests/{request['id']}/cancel"
    )
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["status"] == "cancelled"
    assert request["id"] not in await staff_requests(staff, "new")

    approve = await staff.http.post(
        f"{API}/staff/requests/{request['id']}/approve", json={"tariff": "base"}
    )
    assert is_4xx(approve), approve.text
    reject = await staff.http.post(f"{API}/staff/requests/{request['id']}/reject")
    assert is_4xx(reject), reject.text

    assert await staff_companies(staff) == {}
    assert (await my_requests(applicant))[request["id"]]["status"] == "cancelled"


async def test_foreign_company_request_is_hidden_and_not_cancellable(
    kronto: Kronto,
) -> None:
    """ТЗ §2: заявка — дело заявителя: другой человек её не видит и не
    отменяет."""
    applicant = await register(kronto, f"maria@{DOMAIN}")
    request = await create_request(applicant, "ООО Своя Заявка")
    other, _ = await new_company(kronto, "beta")

    cancel = await other.http.post(
        f"{API}/account/company-requests/{request['id']}/cancel"
    )
    assert cancel.status_code in REFUSED, cancel.text
    listing = await other.http.get(f"{API}/account/company-requests")
    assert listing.status_code == 200, listing.text
    assert request["id"] not in {item["id"] for item in listing.json()}
    assert "ООО Своя Заявка" not in listing.text

    assert (await my_requests(applicant))[request["id"]]["status"] == "new"


async def test_company_request_validation(kronto: Kronto) -> None:
    """ТЗ §2 (правило 5 HARNESS): заявка «Подключить компанию» проверяет ввод —
    пустое и слишком длинное название, места вне 1–10 000, длинный
    комментарий, лишние поля; граничные значения проходят."""
    applicant = await register(kronto, f"maria@{DOMAIN}")
    bad_bodies: list[dict[str, Any]] = [
        {},
        {"company_name": ""},
        {"company_name": "Я"},
        {"company_name": "Я" * 201},
        {"company_name": "ООО Норма", "seats": 0},
        {"company_name": "ООО Норма", "seats": 10001},
        {"company_name": "ООО Норма", "comment": "х" * 2001},
        {"company_name": "ООО Норма", "tariff": "extended"},
    ]
    for body in bad_bodies:
        response = await applicant.http.post(
            f"{API}/account/company-requests", json=body
        )
        assert response.status_code == 422, f"{body}: {response.status_code}"
    assert await my_requests(applicant) == {}

    edge = await create_request(applicant, "ИП", seats=10000, comment="х" * 2000)
    assert edge["company_name"] == "ИП"
    assert edge["seats"] == 10000


async def test_staff_approve_validation(kronto: Kronto) -> None:
    """ТЗ §9, §10: одобрение проверяет ввод — тарифы только Базовый,
    Расширенный, Корпоративный (тарифы лендинга Pro/Max ушли), места 1–10 000;
    неизвестная заявка — 404; ошибка не создаёт компанию."""
    staff = await new_staff(kronto)
    applicant = await register(kronto, f"maria@{DOMAIN}")
    request = await create_request(applicant, "ООО Проверка")

    bad_bodies: list[dict[str, Any]] = [
        {"tariff": "pro"},
        {"tariff": "max"},
        {"tariff": "Pro"},
        {"seats": 0},
        {"seats": 10001},
        {"pilot_until": "через месяц"},
        {"tariff": "base", "name": "Другое название"},
    ]
    for body in bad_bodies:
        response = await staff.http.post(
            f"{API}/staff/requests/{request['id']}/approve", json=body
        )
        assert response.status_code == 422, f"{body}: {response.status_code}"

    unknown = await staff.http.post(
        f"{API}/staff/requests/{rnd()}/approve", json={"tariff": "base"}
    )
    assert unknown.status_code == 404, unknown.text
    unknown = await staff.http.post(f"{API}/staff/requests/{rnd()}/reject")
    assert unknown.status_code == 404, unknown.text
    bad_filter = await staff.http.get(
        f"{API}/staff/requests", params={"status": "approved"}
    )
    assert bad_filter.status_code == 422, bad_filter.text

    assert (await my_requests(applicant))[request["id"]]["status"] == "new"
    assert await staff_companies(staff) == {}


# --------------------------------------------------------------------------
# §9. Компании: тариф, места, пилот, приостановка
# --------------------------------------------------------------------------


async def test_staff_lists_companies(kronto: Kronto) -> None:
    """ТЗ §9: команда видит компании с тарифом, местами, сроком пилота и
    состоянием."""
    staff = await new_staff(kronto)
    _, alfa = await new_company(
        kronto, "alfa", name="ООО Альфа", seats=7, tariff="extended", login_now=False
    )
    _, beta = await new_company(kronto, "beta", name="ООО Бета", login_now=False)

    companies = await staff_companies(staff)
    assert set(companies) == {alfa, beta}
    item = companies[alfa]
    assert item["name"] == "ООО Альфа"
    assert item["company_code"] == "alfa"
    assert item["seats"] == 7
    assert item["tariff"] == "extended"
    assert item["is_active"] is True
    assert item["pilot_until"] is None
    assert item["members"] >= 1
    # допущение: admins — почты администраторов
    assert f"admin.alfa@{DOMAIN}" in " ".join(item["admins"])
    assert companies[beta]["tariff"] == "base"

    detail = await staff_company(staff, alfa)
    assert (detail["seats"], detail["tariff"], detail["name"]) == (
        7,
        "extended",
        "ООО Альфа",
    )
    missing = await staff.http.get(f"{API}/staff/companies/{rnd()}")
    assert missing.status_code == 404, missing.text
    malformed = await staff.http.get(f"{API}/staff/companies/not-a-uuid")
    assert malformed.status_code == 422, malformed.text


async def test_staff_changes_tariff_and_seats(kronto: Kronto) -> None:
    """ТЗ §9, §7: команда меняет тариф и места компании — администратор
    видит новые значения у себя; что не прислано, то не меняется."""
    staff = await new_staff(kronto)
    admin, tenant = await new_company(kronto, "alfa")

    response = await staff_patch(staff, tenant, tariff="enterprise", seats=40)
    assert response.status_code == 200, response.text
    assert (response.json()["tariff"], response.json()["seats"]) == ("enterprise", 40)
    settings = await company_settings(admin)
    assert (settings["tariff"], settings["seats"]) == ("enterprise", 40)

    response = await staff_patch(staff, tenant, seats=41)
    assert response.status_code == 200, response.text
    detail = await staff_company(staff, tenant)
    assert (detail["tariff"], detail["seats"]) == ("enterprise", 41)
    assert detail["is_active"] is True


async def test_staff_sets_and_clears_pilot_period(kronto: Kronto) -> None:
    """ТЗ §2, §9: пробный период выставляется вручную (месяц на тарифе
    «Расширенный»); срок пилота можно снять; другие правки срок не трогают."""
    staff = await new_staff(kronto)
    _, tenant = await new_company(kronto, "alfa", login_now=False)
    pilot = (dt.date.today() + dt.timedelta(days=30)).isoformat()

    response = await staff_patch(staff, tenant, pilot_until=pilot, tariff="extended")
    assert response.status_code == 200, response.text
    assert response.json()["pilot_until"] == pilot
    assert response.json()["tariff"] == "extended"

    response = await staff_patch(staff, tenant, seats=12)
    assert response.status_code == 200, response.text
    assert response.json()["pilot_until"] == pilot

    response = await staff_patch(staff, tenant, pilot_until=None)
    assert response.status_code == 200, response.text
    assert response.json()["pilot_until"] is None
    assert (await staff_company(staff, tenant))["pilot_until"] is None


async def test_staff_company_update_validation(kronto: Kronto) -> None:
    """ТЗ §9, §10: правка компании проверяет ввод — места 1–10 000, только
    тарифы приложения, без лишних полей; неизвестная компания — 404."""
    staff = await new_staff(kronto)
    _, tenant = await new_company(kronto, "alfa", login_now=False)

    bad_bodies: list[dict[str, Any]] = [
        {"seats": 0},
        {"seats": -5},
        {"seats": 10001},
        {"tariff": "pro"},
        {"tariff": "max"},
        {"is_active": "может быть"},
        {"pilot_until": "скоро"},
        {"name": "Новое название"},
    ]
    for body in bad_bodies:
        response = await staff.http.patch(
            f"{API}/staff/companies/{tenant}", json={**body, "confirm": True}
        )
        assert response.status_code == 422, f"{body}: {response.status_code}"

    missing = await staff_patch(staff, rnd(), seats=5)
    assert missing.status_code == 404, missing.text

    detail = await staff_company(staff, tenant)
    assert (detail["seats"], detail["tariff"], detail["is_active"]) == (
        10,
        "base",
        True,
    )


async def test_suspended_company_member_cannot_get_in(kronto: Kronto) -> None:
    """ТЗ §9: команда приостанавливает компанию — её люди при новом входе
    не получают ни документов, ни ответов; модель не вызывается."""
    staff = await new_staff(kronto)
    admin, tenant = await new_company(kronto, "alfa")
    await add_document(
        kronto, admin, "Отпуска", "Отпуск оформляется заявлением за две недели."
    )
    assert (await admin.http.get(f"{API}/materials")).status_code == 200

    response = await staff_patch(staff, tenant, is_active=False)
    assert response.status_code == 200, response.text
    assert response.json()["is_active"] is False
    assert (await staff_company(staff, tenant))["is_active"] is False

    calls = kronto.model.calls
    fresh = await try_login(kronto, admin)
    if fresh is not None:
        materials = await fresh.get(f"{API}/materials")
        assert is_4xx(materials), (
            f"приостановленная компания отдала документы: {materials.text[:200]}"
        )
        switch = await fresh.post(
            f"{API}/auth/switch-company", json={"tenant_id": tenant}
        )
        if switch.status_code == 200:
            set_bearer(fresh, switch.json()["access_token"])
            materials = await fresh.get(f"{API}/materials")
            assert is_4xx(materials), materials.text[:200]
        else:
            assert is_4xx(switch), switch.text
        assert not answered(await ask(fresh, "Как оформить отпуск?"))
    assert kronto.model.calls == calls


async def test_suspension_cuts_existing_sessions(kronto: Kronto) -> None:
    """ТЗ §9: приостановка компании отнимает доступ и у тех, кто уже вошёл:
    администратор не видит документы, сотрудник не получает ответ."""
    staff = await new_staff(kronto)
    admin, tenant = await new_company(kronto, "alfa")
    employee, outcome = await new_employee(kronto, admin, "petr")
    assert outcome == "joined"
    assert (await admin.http.get(f"{API}/materials")).status_code == 200

    response = await staff_patch(staff, tenant, is_active=False)
    assert response.status_code == 200, response.text

    # допущение: доступ пропадает сразу, а не по истечении access-токена —
    # схема обещает мгновенный отзыв токенов при «выйти на устройстве».
    materials = await admin.http.get(f"{API}/materials")
    assert is_4xx(materials), materials.text[:200]
    calls = kronto.model.calls
    reply = await ask(employee.http, "Как оформить отпуск?")
    assert not answered(reply), reply.text[:300]
    assert kronto.model.calls == calls


async def test_resumed_company_regains_access(kronto: Kronto) -> None:
    """ТЗ §9: снятая приостановка возвращает компании доступ."""
    staff = await new_staff(kronto)
    admin, tenant = await new_company(kronto, "alfa")

    assert (await staff_patch(staff, tenant, is_active=False)).status_code == 200
    response = await staff_patch(staff, tenant, is_active=True)
    assert response.status_code == 200, response.text
    assert response.json()["is_active"] is True

    await login(kronto, admin)
    await use_company(admin, tenant)
    materials = await admin.http.get(f"{API}/materials")
    assert materials.status_code == 200, materials.text


async def test_staff_overview_counts(kronto: Kronto) -> None:
    """ТЗ §9: сводка панели — сколько компаний, сколько работает, сколько
    новых заявок, сколько учёток."""
    staff = await new_staff(kronto)
    await new_company(kronto, "alfa", login_now=False)
    _, beta = await new_company(kronto, "beta", login_now=False)
    applicant = await register(kronto, f"maria@{DOMAIN}")
    request = await create_request(applicant, "ООО Новая")
    assert (await staff_patch(staff, beta, is_active=False)).status_code == 200

    response = await staff.http.get(f"{API}/staff/overview")
    assert response.status_code == 200, response.text
    overview = response.json()
    assert overview["companies"] == 2
    assert overview["active_companies"] == 1
    assert overview["requests_new"] == 1
    assert overview["accounts"] >= 4

    assert (
        await staff.http.post(f"{API}/staff/requests/{request['id']}/reject")
    ).status_code == 204
    overview = (await staff.http.get(f"{API}/staff/overview")).json()
    assert overview["requests_new"] == 0


async def test_tariff_change_request_goes_to_team(kronto: Kronto) -> None:
    """ТЗ §7, §10: «сменить тариф» у администратора пишет команде, но тариф
    не меняет — его меняет команда. Тарифы — только Базовый, Расширенный,
    Корпоративный; тарифы лендинга (Pro, Max) не принимаются."""
    staff = await new_staff(kronto)
    admin, tenant = await new_company(kronto, "alfa")

    response = await admin.http.post(
        f"{API}/company/tariff-request",
        json={"tariff": "extended", "seats": 20, "comment": "Хотим расширенный"},
    )
    assert response.status_code == 202, response.text
    settings = await company_settings(admin)
    assert (settings["tariff"], settings["seats"]) == ("base", 10)

    for bad in ("pro", "max", "Pro", "enterprise_plus"):
        response = await admin.http.post(
            f"{API}/company/tariff-request", json={"tariff": bad}
        )
        assert response.status_code == 422, f"{bad}: {response.status_code}"

    response = await staff_patch(staff, tenant, tariff="extended", seats=20)
    assert response.status_code == 200, response.text
    settings = await company_settings(admin)
    assert (settings["tariff"], settings["seats"]) == ("extended", 20)


# --------------------------------------------------------------------------
# §9. Расход на модели
# --------------------------------------------------------------------------


async def test_staff_spend_counts_company_questions(kronto: Kronto) -> None:
    """ТЗ §9: расход на модели — общий и по компаниям, по дням; вопрос
    компании в нём учтён."""
    staff = await new_staff(kronto)
    admin, tenant = await new_company(kronto, "alfa")
    await add_document(
        kronto, admin, "Отпуска", "Отпуск оформляется заявлением за две недели."
    )
    reply = await ask(admin.http, "Как оформляется отпуск?")
    assert reply.status_code == 200, reply.text
    await kronto.run_background()

    response = await staff.http.get(f"{API}/staff/spend")
    assert response.status_code == 200, response.text
    spend = response.json()
    # допущение: ответ заглушки модели пишется в журнал ответов, как в бою
    assert spend["questions"] >= 1
    rows = [row for row in spend["companies"] if row["tenant_id"] == tenant]
    assert len(rows) == 1
    assert rows[0]["questions"] >= 1
    assert rows[0]["company_code"] == "alfa"
    assert spend["since"] <= spend["until"]
    assert sum(day["questions"] for day in spend["days"]) >= 1

    week = await staff.http.get(f"{API}/staff/spend", params={"days": 7})
    assert week.status_code == 200, week.text
    for bad in ("0", "91", "неделя"):
        response = await staff.http.get(f"{API}/staff/spend", params={"days": bad})
        assert response.status_code == 422, f"days={bad}: {response.status_code}"


# --------------------------------------------------------------------------
# §9. Помощь со входом
# --------------------------------------------------------------------------


async def test_staff_finds_person_for_login_help_without_secrets(
    kronto: Kronto,
) -> None:
    """ТЗ §9: поиск человека для помощи со входом — видно, что настроено
    (почта, приложение, компании), но не секреты: ни пароля, ни секрета
    приложения."""
    staff = await new_staff(kronto)
    admin, tenant = await new_company(kronto, "alfa", name="ООО Альфа")

    response = await staff.http.get(f"{API}/staff/people", params={"q": "admin.alfa"})
    assert response.status_code == 200, response.text
    found = [person for person in response.json() if person["email"] == admin.email]
    assert len(found) == 1
    person = found[0]
    assert person["totp"] is True
    assert person["email_verified"] is True
    assert person["staff"] is False
    assert any(
        item["tenant_id"] == tenant and item["role"] == "admin"
        for item in person["companies"]
    )

    detail = await staff.http.get(f"{API}/staff/people/{person['id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["email"] == admin.email
    assert admin.totp
    for text in (response.text, detail.text):
        assert admin.password not in text
        assert admin.totp not in text
    for key in detail.json():
        assert (
            "hash" not in key and "secret" not in key and not key.startswith("password")
        ), key

    by_name = await staff.http.get(f"{API}/staff/people", params={"q": "Артём"})
    assert by_name.status_code == 200, by_name.text
    assert any(
        item["email"] == STAFF_EMAIL and item["staff"] is True
        for item in by_name.json()
    )


async def test_staff_people_search_rules(kronto: Kronto) -> None:
    """ТЗ §9: поиск человека — от трёх символов; неизвестный человек — 404."""
    staff = await new_staff(kronto)
    await new_company(kronto, "alfa", login_now=False)

    short = await staff.http.get(f"{API}/staff/people", params={"q": "ad"})
    # допущение: короткий запрос — 422 или пустой список, но не выдача людей
    assert short.status_code == 422 or (
        short.status_code == 200 and short.json() == []
    ), short.text
    too_long = await staff.http.get(f"{API}/staff/people", params={"q": "a" * 255})
    assert too_long.status_code == 422, too_long.text
    nobody = await staff.http.get(f"{API}/staff/people", params={"q": "никтотакой"})
    assert nobody.status_code == 200 and nobody.json() == [], nobody.text
    missing = await staff.http.get(f"{API}/staff/people/{rnd()}")
    assert missing.status_code == 404, missing.text


async def test_staff_password_reset_link_reaches_only_the_person(
    kronto: Kronto,
) -> None:
    """ТЗ §9, §3: команда помогает со входом — отправляет человеку ссылку
    на новый пароль. Пароль команда не видит и не задаёт: ссылка приходит
    только человеку, по ней он ставит пароль сам."""
    staff = await new_staff(kronto)
    target, _ = await new_company(kronto, "alfa", totp=False, login_now=False)
    old_password = target.password

    search = await staff.http.get(f"{API}/staff/people", params={"q": target.email})
    assert search.status_code == 200, search.text
    matches = [item["id"] for item in search.json() if item["email"] == target.email]
    assert len(matches) == 1, search.text
    account_id = matches[0]
    target_before = len(await kronto.inbox(target.email))
    staff_before = len(await kronto.inbox(STAFF_EMAIL))

    response = await staff.http.post(f"{API}/staff/people/{account_id}/password-reset")
    assert response.status_code == 202, response.text

    letters = (await kronto.inbox(target.email))[target_before:]
    assert letters, "человеку не пришло письмо со ссылкой"
    tokens = link_tokens(letters)
    assert tokens, "в письме нет ссылки с токеном"
    for token in tokens:
        assert token not in response.text, "панель увидела токен сброса пароля"
    assert len(await kronto.inbox(STAFF_EMAIL)) == staff_before

    new = new_password()
    reset_ok = False
    for token in tokens:
        reset = await kronto.browser().post(
            f"{API}/auth/reset-password", json={"token": token, "new_password": new}
        )
        if reset.status_code == 200:
            reset_ok = True
            break
    assert reset_ok, "ссылка из письма не сбрасывает пароль"

    _, old_login = await start_login(kronto, target, password=old_password)
    assert is_4xx(old_login), "старый пароль всё ещё действует"
    _, new_login = await start_login(kronto, target, password=new)
    assert new_login.status_code == 200, new_login.text


# --------------------------------------------------------------------------
# §6, §9. Панель не раскрывает вопросы и документы компаний
# --------------------------------------------------------------------------


async def test_staff_panel_does_not_show_questions_or_documents(kronto: Kronto) -> None:
    """ТЗ §6, §9: свои диалоги видит только сам человек — команда в панели
    видит цифры (вопросы, документы, расход), но не тексты вопросов и не
    содержимое документов компаний."""
    staff = await new_staff(kronto)
    admin, tenant = await new_company(kronto, "alfa")
    employee, _ = await new_employee(kronto, admin, "petr")
    employee_id = (await me_of(employee.http))["id"]

    question_marker = "КВАНТОВЫЙБАРСУК5512"
    content_marker = "ТАЙНЫЙКЛЮЧ9087"
    title_marker = "Регламент ОМЕГА7741"
    await add_document(
        kronto,
        admin,
        title_marker,
        f"Порядок отпусков компании. Код доступа {content_marker}.",
    )
    reply = await ask(employee.http, f"Порядок отпусков компании и {question_marker}?")
    assert reply.status_code == 200, reply.text
    await kronto.run_background()

    reads: list[tuple[str, dict[str, Any] | None]] = [
        ("/staff/overview", None),
        ("/staff/companies", None),
        (f"/staff/companies/{tenant}", None),
        ("/staff/spend", None),
        ("/staff/spend", {"days": 90}),
        ("/staff/requests", {"status": "all"}),
        ("/staff/support", None),
        ("/staff/leads", None),
        ("/staff/people", {"q": employee.email}),
        ("/staff/people", {"q": "admin.alfa"}),
        (f"/staff/people/{employee_id}", None),
    ]
    for path, params in reads:
        response = await staff.http.get(f"{API}{path}", params=params)
        assert response.status_code == 200, f"{path}: {response.text}"
        assert question_marker not in response.text, f"{path}: текст вопроса в панели"
        # допущение: ТЗ §9 не перечисляет документы среди функций панели —
        # их содержимое и названия в ней не нужны
        assert content_marker not in response.text, (
            f"{path}: содержимое документа в панели"
        )
        assert title_marker not in response.text, f"{path}: название документа в панели"


# --------------------------------------------------------------------------
# §8, §9. «Написать в поддержку»
# --------------------------------------------------------------------------


async def test_support_request_reaches_staff_panel(kronto: Kronto) -> None:
    """ТЗ §8, §9: «Написать в поддержку» — обращение видно автору и команде
    (с почтой и компанией автора)."""
    staff = await new_staff(kronto)
    admin, _ = await new_company(kronto, "alfa", name="ООО Альфа")

    response = await admin.http.post(
        f"{API}/support",
        json={"topic": "login", "message": "Не приходит код на почту при входе"},
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["status"] == "new"
    assert created["topic"] == "login"
    assert created["message"] == "Не приходит код на почту при входе"
    assert created["id"] in await support_mine(admin)

    listing = await staff.http.get(f"{API}/staff/support")
    assert listing.status_code == 200, listing.text
    rows = [row for row in listing.json() if row["id"] == created["id"]]
    assert len(rows) == 1, listing.text
    item = rows[0]
    assert item["message"] == "Не приходит код на почту при входе"
    assert item["email"] == admin.email
    assert item["status"] == "new"
    assert item["topic"] == "login"
    # допущение: company — название (или код) компании автора
    assert item["company"] in ("ООО Альфа", "alfa")


async def test_support_status_change_visible_to_author(kronto: Kronto) -> None:
    """ТЗ §8, §9: команда отмечает обращение отвеченным — автор видит новый
    статус; фильтр панели по статусу работает; ввод проверяется."""
    staff = await new_staff(kronto)
    admin, _ = await new_company(kronto, "alfa")
    created = await admin.http.post(
        f"{API}/support",
        json={"topic": "billing", "message": "Как получить счёт для юрлица?"},
    )
    assert created.status_code == 201, created.text
    support_id = created.json()["id"]

    response = await staff.http.patch(
        f"{API}/staff/support/{support_id}", json={"status": "answered"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "answered"
    assert (await support_mine(admin))[support_id]["status"] == "answered"

    new_only = await staff.http.get(f"{API}/staff/support", params={"status": "new"})
    assert support_id not in {row["id"] for row in new_only.json()}
    answered_only = await staff.http.get(
        f"{API}/staff/support", params={"status": "answered"}
    )
    assert support_id in {row["id"] for row in answered_only.json()}

    bad = await staff.http.patch(
        f"{API}/staff/support/{support_id}", json={"status": "done"}
    )
    assert bad.status_code == 422, bad.text
    bad_filter = await staff.http.get(f"{API}/staff/support", params={"status": "done"})
    assert bad_filter.status_code == 422, bad_filter.text
    missing = await staff.http.patch(
        f"{API}/staff/support/{rnd()}", json={"status": "closed"}
    )
    assert missing.status_code == 404, missing.text


async def test_support_requests_are_private(kronto: Kronto) -> None:
    """ТЗ §8: обращение в поддержку видит его автор и команда — не другие
    люди и не другие компании."""
    alfa_admin, _ = await new_company(kronto, "alfa")
    beta_admin, _ = await new_company(kronto, "beta")
    created = await alfa_admin.http.post(
        f"{API}/support",
        json={"topic": "documents", "message": "Не загружается договор поставки"},
    )
    assert created.status_code == 201, created.text
    support_id = created.json()["id"]

    beta_mine = await beta_admin.http.get(f"{API}/support/mine")
    assert beta_mine.status_code == 200, beta_mine.text
    assert support_id not in {row["id"] for row in beta_mine.json()}
    assert "договор поставки" not in beta_mine.text

    change = await beta_admin.http.patch(
        f"{API}/staff/support/{support_id}", json={"status": "closed"}
    )
    assert change.status_code in REFUSED, change.text
    assert (await support_mine(alfa_admin))[support_id]["status"] == "new"


async def test_support_request_validation(kronto: Kronto) -> None:
    """ТЗ §8 (правило 5 HARNESS): обращение — с темой из списка и текстом
    10–4 000 символов, не из одних пробелов; ошибка ничего не сохраняет."""
    admin, _ = await new_company(kronto, "alfa")
    bad_bodies: list[dict[str, Any]] = [
        {},
        {"topic": "login"},
        {"topic": "login", "message": "коротко"},
        {"topic": "login", "message": " " * 20},
        {"topic": "login", "message": "я" * 4001},
        {"topic": "spam", "message": "Нормальное длинное сообщение"},
        {
            "topic": "login",
            "message": "Нормальное длинное сообщение",
            "email": "x@bb-kronto.ru",
        },
    ]
    for body in bad_bodies:
        response = await admin.http.post(f"{API}/support", json=body)
        assert response.status_code == 422, f"{body}: {response.status_code}"
    assert await support_mine(admin) == {}

    edge = await admin.http.post(
        f"{API}/support", json={"topic": "other", "message": "я" * 4000}
    )
    assert edge.status_code == 201, edge.text


# --------------------------------------------------------------------------
# §9, §10. Запись на созвон (в тестовой среде выключена)
# --------------------------------------------------------------------------


async def test_call_booking_is_off_in_test_env(kronto: Kronto) -> None:
    """ТЗ §9, §10, HARNESS: запись на созвон со страницы тарифов в тестовой
    среде выключена — форма говорит «выключено», заявка не принимается, в
    панели заявок нет; фильтры панели проверяют ввод."""
    anonymous = kronto.browser()
    form = await anonymous.get(f"{API}/leads/form")
    assert form.status_code == 200, form.text
    assert form.json()["enabled"] is False

    slots = form.json().get("slots") or ["10:00"]
    lead = {
        "company_name": "ООО Звонок",
        "contact_name": "Иван Звонов",
        "phone": "+79991234567",
        "email": f"ivan@{DOMAIN}",
        "seats": 20,
        "tariff": "extended",
        "preferred_date": form.json()["first_date"],
        "preferred_slot": slots[0],
        "consent": True,
        "policy_version": form.json().get("policy_version") or "1",
    }
    response = await anonymous.post(f"{API}/leads", json=lead)
    # допущение: выключенная форма отвечает отказом (4xx или 503), не 201
    assert response.status_code != 201 and response.status_code != 500, response.text

    staff = await new_staff(kronto)
    leads = await staff.http.get(f"{API}/staff/leads")
    assert leads.status_code == 200 and leads.json() == [], leads.text
    filtered = await staff.http.get(f"{API}/staff/leads", params={"status": "new"})
    assert filtered.status_code == 200 and filtered.json() == [], filtered.text
    bad_filter = await staff.http.get(f"{API}/staff/leads", params={"status": "won"})
    assert bad_filter.status_code == 422, bad_filter.text
    missing = await staff.http.patch(
        f"{API}/staff/leads/{rnd()}", json={"status": "contacted"}
    )
    assert missing.status_code == 404, missing.text
    bad_status = await staff.http.patch(
        f"{API}/staff/leads/{rnd()}", json={"status": "won"}
    )
    assert bad_status.status_code == 422, bad_status.text


# --------------------------------------------------------------------------
# §8. Уведомления: колокольчик и письма
# --------------------------------------------------------------------------


async def test_join_request_notifies_admin_in_bell_and_by_email(kronto: Kronto) -> None:
    """ТЗ §8, §2: заявка на вступление (приглашение с одобрением) —
    администратору в колокольчик и письмом, если письма включены."""
    admin, _ = await new_company(kronto, "alfa")
    settings = await set_notify(admin, email_join_requests=True)
    assert settings["email_join_requests"] is True
    before = len(await kronto.inbox(admin.email))

    _, outcome = await new_employee(kronto, admin, "petr", approval=True)
    assert outcome == "pending"

    notifications = await bell(admin)
    joins = [item for item in notifications["items"] if item["kind"] == "join_request"]
    assert len(joins) == 1
    assert joins[0]["read"] is False
    assert joins[0]["title"].strip()
    assert notifications["unread"] >= 1
    letters = (await kronto.inbox(admin.email))[before:]
    assert letters, "администратору не пришло письмо о заявке на вступление"


async def test_join_request_email_can_be_turned_off(kronto: Kronto) -> None:
    """ТЗ §8: что слать письмом — в настройках. Письма о заявках выключены —
    письма нет, а колокольчик приходит всё равно."""
    admin, _ = await new_company(kronto, "alfa")
    settings = await set_notify(admin, email_join_requests=False)
    assert settings["email_join_requests"] is False
    before = len(await kronto.inbox(admin.email))

    _, outcome = await new_employee(kronto, admin, "petr", approval=True)
    assert outcome == "pending"

    assert len(await kronto.inbox(admin.email)) == before, (
        "письмо ушло при выключенной настройке"
    )
    notifications = await bell(admin)
    assert any(item["kind"] == "join_request" for item in notifications["items"])


async def test_join_request_not_shown_to_employee(kronto: Kronto) -> None:
    """ТЗ §8: уведомления о заявках на вступление — администратору, не
    сотруднику."""
    admin, _ = await new_company(kronto, "alfa")
    colleague, outcome = await new_employee(kronto, admin, "colleague")
    assert outcome == "joined"
    _, outcome = await new_employee(
        kronto, admin, "newbie", first="Олег", last="Новичков", approval=True
    )
    assert outcome == "pending"

    assert any(item["kind"] == "join_request" for item in (await bell(admin))["items"])
    response = await colleague.http.get(f"{API}/notifications")
    if response.status_code == 200:
        assert all(item["kind"] != "join_request" for item in response.json()["items"])
    else:
        assert is_4xx(response), response.text


async def test_mark_notifications_read(kronto: Kronto) -> None:
    """ТЗ §8: колокольчик — непрочитанные; можно отметить одно или все."""
    admin, _ = await new_company(kronto, "alfa")
    await new_employee(
        kronto, admin, "first", first="Олег", last="Первый", approval=True
    )
    await new_employee(
        kronto, admin, "second", first="Ольга", last="Вторая", approval=True
    )

    notifications = await bell(admin)
    joins = [item for item in notifications["items"] if item["kind"] == "join_request"]
    assert len(joins) == 2
    unread = notifications["unread"]
    assert unread >= 2

    response = await admin.http.post(
        f"{API}/notifications/read", json={"ids": [joins[0]["id"]]}
    )
    assert response.status_code == 200, response.text
    after = response.json()
    states = {item["id"]: item["read"] for item in after["items"]}
    assert states[joins[0]["id"]] is True
    assert states[joins[1]["id"]] is False
    assert after["unread"] == unread - 1
    assert (await bell(admin))["unread"] == unread - 1

    response = await admin.http.post(f"{API}/notifications/read", json={})
    assert response.status_code == 200, response.text
    assert response.json()["unread"] == 0
    assert all(item["read"] for item in (await bell(admin))["items"])


async def test_cannot_mark_other_admins_notifications(kronto: Kronto) -> None:
    """ТЗ §8, §2: уведомления — свои: администратор другой компании их не
    видит и не отмечает прочитанными."""
    alfa_admin, _ = await new_company(kronto, "alfa")
    beta_admin, _ = await new_company(kronto, "beta")
    await new_employee(kronto, alfa_admin, "petr", approval=True)
    alfa_before = await bell(alfa_admin)
    joins = [item for item in alfa_before["items"] if item["kind"] == "join_request"]
    assert len(joins) == 1, alfa_before
    notification = joins[0]
    unread = alfa_before["unread"]

    beta_bell = await bell(beta_admin)
    assert notification["id"] not in {item["id"] for item in beta_bell["items"]}
    response = await beta_admin.http.post(
        f"{API}/notifications/read", json={"ids": [notification["id"]]}
    )
    assert response.status_code != 500, response.text
    if response.status_code == 200:
        assert notification["id"] not in {
            item["id"] for item in response.json()["items"]
        }

    alfa_bell = await bell(alfa_admin)
    assert {item["id"]: item["read"] for item in alfa_bell["items"]}[
        notification["id"]
    ] is False
    assert alfa_bell["unread"] == unread


async def test_notification_settings_update_and_validation(kronto: Kronto) -> None:
    """ТЗ §8, §4: настройки «Уведомления» — что прислано, то и меняется,
    сохраняется, у другого человека не меняется; неверный ввод — 422."""
    alfa_admin, _ = await new_company(kronto, "alfa")
    beta_admin, _ = await new_company(kronto, "beta")

    response = await alfa_admin.http.get(f"{API}/notifications/settings")
    assert response.status_code == 200, response.text
    original = response.json()
    assert set(original) >= {
        "email_connectors",
        "email_credits",
        "email_join_requests",
        "email_weekly_digest",
    }
    assert all(isinstance(value, bool) for value in original.values())

    flipped = not original["email_weekly_digest"]
    updated = await set_notify(alfa_admin, email_weekly_digest=flipped)
    assert updated["email_weekly_digest"] is flipped
    for key in ("email_connectors", "email_credits", "email_join_requests"):
        assert updated[key] == original[key], key
    stored = (await alfa_admin.http.get(f"{API}/notifications/settings")).json()
    assert stored["email_weekly_digest"] is flipped
    other = (await beta_admin.http.get(f"{API}/notifications/settings")).json()
    assert other == original

    for body in ({"email_weekly_digest": "может быть"}, {"email_sms": True}):
        bad = await alfa_admin.http.put(f"{API}/notifications/settings", json=body)
        assert bad.status_code == 422, f"{body}: {bad.status_code}"
    for body in ({"ids": ["не-uuid"]}, {"ids": [rnd() for _ in range(101)]}):
        bad = await alfa_admin.http.post(f"{API}/notifications/read", json=body)
        assert bad.status_code == 422, f"read {str(body)[:40]}: {bad.status_code}"
    stored = (await alfa_admin.http.get(f"{API}/notifications/settings")).json()
    assert stored["email_weekly_digest"] is flipped


async def test_weekly_digest_reaches_admin(kronto: Kronto) -> None:
    """ТЗ §8: недельная сводка — администратору письмом (и в колокольчик)."""
    admin, _ = await new_company(kronto, "alfa")
    await set_notify(admin, email_weekly_digest=True)
    await add_document(
        kronto, admin, "Отпуска", "Отпуск оформляется заявлением за две недели."
    )
    assert (await ask(admin.http, "Как оформляется отпуск?")).status_code == 200
    await kronto.run_background()
    before = len(await kronto.inbox(admin.email))

    sent = await kronto.send_digest(company_code="alfa")
    assert sent >= 1
    letters = (await kronto.inbox(admin.email))[before:]
    assert letters, "администратору не пришла недельная сводка"
    # допущение: сводка приходит и в колокольчик («колокольчик и письма»)
    assert any(item["kind"] == "weekly_digest" for item in (await bell(admin))["items"])


async def test_weekly_digest_respects_setting(kronto: Kronto) -> None:
    """ТЗ §8: письма — по настройкам: недельная сводка выключена — письма
    нет."""
    admin, _ = await new_company(kronto, "alfa")
    await set_notify(admin, email_weekly_digest=False)
    await add_document(
        kronto, admin, "Отпуска", "Отпуск оформляется заявлением за две недели."
    )
    assert (await ask(admin.http, "Как оформляется отпуск?")).status_code == 200
    await kronto.run_background()
    before = len(await kronto.inbox(admin.email))

    await kronto.send_digest(company_code="alfa")
    assert len(await kronto.inbox(admin.email)) == before, (
        "сводка ушла при выключенной настройке"
    )
    # Разбор 06.10: send_digest считает компании, а не письма (HARNESS.md
    # описывал неточно) — проверка числа писем выше и есть требование ТЗ.


async def test_weekly_digest_not_sent_to_employee(kronto: Kronto) -> None:
    """ТЗ §8: сотруднику письма — только о безопасности: недельную сводку он
    не получает, даже если попытается её включить."""
    admin, _ = await new_company(kronto, "alfa")
    await set_notify(admin, email_weekly_digest=True)
    employee, outcome = await new_employee(kronto, admin, "petr")
    assert outcome == "joined"
    attempt = await employee.http.put(
        f"{API}/notifications/settings", json={"email_weekly_digest": True}
    )
    assert attempt.status_code != 500, attempt.text

    await add_document(
        kronto, admin, "Отпуска", "Отпуск оформляется заявлением за две недели."
    )
    assert (await ask(employee.http, "Как оформляется отпуск?")).status_code == 200
    await kronto.run_background()
    admin_before = len(await kronto.inbox(admin.email))
    employee_before = len(await kronto.inbox(employee.email))

    await kronto.send_digest(company_code="alfa")
    assert len(await kronto.inbox(employee.email)) == employee_before, (
        "сотруднику ушла сводка"
    )
    assert len(await kronto.inbox(admin.email)) > admin_before, (
        "сводка не ушла и администратору"
    )


# --------------------------------------------------------------------------
# §8. Первые шаги
# --------------------------------------------------------------------------


async def test_onboarding_new_admin_starts_with_empty_checklist(kronto: Kronto) -> None:
    """ТЗ §8: у администратора новой компании — чек-лист, в нём ничего не
    сделано; неизвестный шаг — 422."""
    admin, _ = await new_company(kronto, "alfa")
    state = await onboarding(admin)
    assert state == {
        "checklist_hidden": False,
        "documents": False,
        "people": False,
        "question": False,
        "tips_seen": False,
    }
    bad = await admin.http.post(f"{API}/onboarding/documents")
    assert bad.status_code == 422, bad.text
    assert await onboarding(admin) == state


async def test_onboarding_checklist_follows_admin_actions(kronto: Kronto) -> None:
    """ТЗ §8: чек-лист администратора — загрузить файлы → пригласить людей →
    задать первый вопрос; пункты отмечаются по делу, а не вручную."""
    admin, _ = await new_company(kronto, "alfa")

    await add_document(
        kronto, admin, "Отпуска", "Отпуск оформляется заявлением за две недели."
    )
    state = await onboarding(admin)
    assert state["documents"] is True
    assert state["question"] is False

    assert (await ask(admin.http, "Как оформляется отпуск?")).status_code == 200
    await kronto.run_background()
    assert (await onboarding(admin))["question"] is True

    # допущение: «пригласить людей» засчитывается не позже, чем человек вступил
    _, outcome = await new_employee(kronto, admin, "petr")
    assert outcome == "joined"
    state = await onboarding(admin)
    assert (state["documents"], state["people"], state["question"]) == (
        True,
        True,
        True,
    )


async def test_onboarding_checklist_can_be_hidden(kronto: Kronto) -> None:
    """ТЗ §8: чек-лист первых шагов можно скрыть, и он остаётся скрытым."""
    admin, _ = await new_company(kronto, "alfa")
    response = await admin.http.post(f"{API}/onboarding/checklist")
    assert response.status_code == 200, response.text
    assert response.json()["checklist_hidden"] is True
    assert (await onboarding(admin))["checklist_hidden"] is True


async def test_onboarding_tips_are_per_person(kronto: Kronto) -> None:
    """ТЗ §8: сотруднику — подсказки при первом входе; отметка «показаны» —
    его собственная и не трогает других."""
    admin, _ = await new_company(kronto, "alfa")
    employee, _ = await new_employee(kronto, admin, "petr")

    assert (await onboarding(employee))["tips_seen"] is False
    response = await employee.http.post(f"{API}/onboarding/tips")
    assert response.status_code == 200, response.text
    assert response.json()["tips_seen"] is True
    assert (await onboarding(employee))["tips_seen"] is True
    assert (await onboarding(admin))["tips_seen"] is False


# --------------------------------------------------------------------------
# §1, §2. Что публично, а что нет
# --------------------------------------------------------------------------


async def test_public_endpoints_open_without_login(kronto: Kronto) -> None:
    """ТЗ §1: гость попадает на сайт без входа — корень и форма записи на
    созвон со страницы тарифов открыты всем."""
    anonymous = kronto.browser()
    root = await anonymous.get("/")
    assert root.status_code == 200, root.text
    assert isinstance(root.json(), dict)

    form = await anonymous.get(f"{API}/leads/form")
    assert form.status_code == 200, form.text
    body = form.json()
    assert isinstance(body["enabled"], bool)
    assert body["first_date"] <= body["last_date"]
    assert body["timezone"]


async def test_account_features_require_login(kronto: Kronto) -> None:
    """ТЗ §1, §2, §8: колокольчик, первые шаги, поддержка, заявка
    «Подключить компанию», смена тарифа — только после входа; без входа
    ничего не сохраняется."""
    anonymous = kronto.browser()
    calls: list[tuple[str, str, dict[str, Any] | None]] = [
        ("GET", "/notifications", None),
        ("POST", "/notifications/read", {}),
        ("GET", "/notifications/settings", None),
        ("PUT", "/notifications/settings", {"email_weekly_digest": False}),
        ("GET", "/onboarding", None),
        ("POST", "/onboarding/tips", None),
        ("POST", "/onboarding/checklist", None),
        (
            "POST",
            "/support",
            {"topic": "login", "message": "Не могу войти в учётную запись"},
        ),
        ("GET", "/support/mine", None),
        ("GET", "/account/company-requests", None),
        ("POST", "/account/company-requests", {"company_name": "ООО Аноним"}),
        ("POST", f"/account/company-requests/{rnd()}/cancel", None),
        ("POST", "/company/tariff-request", {"tariff": "extended"}),
    ]
    # допущение: без входа — 401 (или 403)
    await assert_all_refused(anonymous, calls, NO_LOGIN, "без входа")

    staff = await new_staff(kronto)
    assert await staff_requests(staff, "all") == {}
    support = await staff.http.get(f"{API}/staff/support")
    assert support.status_code == 200 and support.json() == [], support.text


# --------------------------------------------------------------------------
# §1, §2. Демо-песочница
# --------------------------------------------------------------------------


async def test_demo_is_off_until_set_up(kronto: Kronto) -> None:
    """ТЗ §1, HARNESS: песочница отвечает, только когда заведена её
    вымышленная компания; до этого — 503 demo_off, модель не вызывается."""
    anonymous = kronto.browser()
    calls = kronto.model.calls
    info = await anonymous.get(f"{API}/demo")
    assert info.status_code == 503, info.text
    for path in ("/demo/ask", "/demo/ask/stream"):
        response = await anonymous.post(
            f"{API}{path}", json={"question": "Как оформить отпуск?"}
        )
        assert response.status_code == 503, f"{path}: {response.status_code}"
    assert kronto.model.calls == calls


async def test_demo_info_is_public(kronto: Kronto) -> None:
    """ТЗ §1, §2: демо с переходом в песочницу — без входа: вымышленная
    компания, её документы и готовые вопросы."""
    await kronto.setup_demo()
    anonymous = kronto.browser()
    response = await anonymous.get(f"{API}/demo")
    assert response.status_code == 200, response.text
    info = response.json()
    assert info["company"].strip()
    assert info["documents"] and all(
        isinstance(title, str) and title.strip() for title in info["documents"]
    )
    assert info["questions"] and all(
        isinstance(q, str) and q.strip() for q in info["questions"]
    )


async def test_demo_answers_ready_question_from_demo_documents(kronto: Kronto) -> None:
    """ТЗ §1, §2: в песочнице без входа задают вопрос по демо-документам и
    получают ответ со ссылками на них."""
    await kronto.setup_demo()
    anonymous = kronto.browser()
    info = (await anonymous.get(f"{API}/demo")).json()

    response = await anonymous.post(
        f"{API}/demo/ask", json={"question": info["questions"][0]}
    )
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["content"].strip()
    # допущение: готовый вопрос песочницы находит ответ в её документах
    assert answer["origin"] == "documents"
    assert answer["sources"]
    # допущение: documents в /demo — названия документов, те же, что в источниках
    for source in answer["sources"]:
        assert source["title"] in info["documents"], source["title"]


async def test_demo_stream_prints_answer(kronto: Kronto) -> None:
    """ТЗ §1, §6: в песочнице ответ печатается по мере генерации, как в
    чате: кусочки текста, затем итог."""
    await kronto.setup_demo()
    anonymous = kronto.browser()
    question = (await anonymous.get(f"{API}/demo")).json()["questions"][0]

    response = await anonymous.post(
        f"{API}/demo/ask/stream", json={"question": question}
    )
    assert response.status_code == 200, response.text
    assert response.headers.get("content-type", "").startswith("text/event-stream")
    events = sse_events(response.text)
    types = [event.get("type") for event in events]
    assert "error" not in types, response.text[:500]
    assert types and types[-1] == "done"
    # допущение: заглушка модели отдаёт ответ по словам — кусочков несколько
    assert types.count("delta") >= 2
    printed = "".join(event["text"] for event in events if event.get("type") == "delta")
    assert printed.strip()
    assert events[-1]["answer"]["content"].strip()


async def test_demo_never_leaks_real_company_documents(kronto: Kronto) -> None:
    """ТЗ §1, §2, §6: песочница отвечает только про вымышленную компанию —
    документы настоящих компаний в её ответы и источники не попадают, даже
    если спрашивает вошедший администратор такой компании."""
    admin, _ = await new_company(kronto, "alfa", name="ООО Альфа-Секрет")
    marker = "ФИОЛЕТОВЫЙЖИРАФ4821"
    await add_document(
        kronto,
        admin,
        "Склад Альфы",
        f"Пароль от склада компании: {marker}. Склад открывается в девять утра.",
    )
    question = "Какой пароль от склада компании?"
    guard = await ask(admin.http, question)
    assert guard.status_code == 200, guard.text
    assert marker in guard.text, "предусловие: свой поиск компании находит документ"

    await kronto.setup_demo()
    anonymous = kronto.browser()
    info = await anonymous.get(f"{API}/demo")
    assert info.status_code == 200, info.text
    assert info.json()["company"] != "ООО Альфа-Секрет"
    assert "Склад Альфы" not in info.json()["documents"]

    for client in (anonymous, admin.http):
        for path in ("/demo/ask", "/demo/ask/stream"):
            response = await client.post(f"{API}{path}", json={"question": question})
            assert response.status_code == 200, f"{path}: {response.text}"
            assert marker not in response.text, (
                f"{path}: песочница выдала документ настоящей компании"
            )
            assert "Склад Альфы" not in response.text
            assert "Альфа-Секрет" not in response.text


async def test_demo_has_question_limit(kronto: Kronto) -> None:
    """ТЗ §2: демо-песочница — с лимитом вопросов; сверх лимита — отказ без
    вызова модели, и новый браузер с того же адреса лимит не обходит."""
    await kronto.setup_demo()
    anonymous = kronto.browser()
    base = (await anonymous.get(f"{API}/demo")).json()["questions"][0][:250]

    answered_count = 0
    refused: httpx.Response | None = None
    for number in range(60):
        response = await anonymous.post(
            f"{API}/demo/ask", json={"question": f"{base} (вариант {number})"}
        )
        if response.status_code != 200:
            refused = response
            break
        answered_count += 1
    assert refused is not None, "за 60 вопросов лимит песочницы не сработал"
    assert answered_count >= 1, (
        f"песочница не ответила ни разу: {refused.status_code} {refused.text}"
    )
    # допущение: превышение — 429 (или 503 при исчерпании общего суточного пула)
    assert refused.status_code in (429, 503), refused.text

    calls = kronto.model.calls
    again = await anonymous.post(
        f"{API}/demo/ask", json={"question": f"{base} (ещё раз)"}
    )
    assert again.status_code in (429, 503), again.text
    other = await kronto.browser().post(
        f"{API}/demo/ask", json={"question": f"{base} (другой браузер)"}
    )
    assert other.status_code in (429, 503), other.text
    assert kronto.model.calls == calls


async def test_demo_question_validation(kronto: Kronto) -> None:
    """ТЗ §1, §2 (правило 5 HARNESS): вопрос песочницы — 3–300 символов, без
    лишних полей; неверный ввод модель не вызывает."""
    await kronto.setup_demo()
    anonymous = kronto.browser()
    calls = kronto.model.calls
    bad_bodies: list[dict[str, Any]] = [
        {},
        {"question": ""},
        {"question": "аб"},
        {"question": "я" * 301},
        {"question": "Как оформить отпуск?", "company": "alfa"},
    ]
    for body in bad_bodies:
        for path in ("/demo/ask", "/demo/ask/stream"):
            response = await anonymous.post(f"{API}{path}", json=body)
            assert response.status_code == 422, (
                f"{path} {str(body)[:40]}: {response.status_code}"
            )
    assert kronto.model.calls == calls


async def test_demo_bot_trap_does_not_reach_model(kronto: Kronto) -> None:
    """ТЗ §1, §2: песочница без входа защищена от ботов: заполненное
    скрытое поле website не тратит вызов модели; обычный вопрос — тратит."""
    await kronto.setup_demo()
    anonymous = kronto.browser()
    question = (await anonymous.get(f"{API}/demo")).json()["questions"][0][:250]

    calls = kronto.model.calls
    trap = await anonymous.post(
        f"{API}/demo/ask",
        json={
            "question": f"{question} Подробнее.",
            "website": "https://spam.bb-kronto.ru",
        },
    )
    # допущение: код ответа ловушки ТЗ не задаёт; главное — модель не вызвана
    assert trap.status_code < 500, trap.text
    assert kronto.model.calls == calls

    real = await kronto.browser().post(
        f"{API}/demo/ask", json={"question": f"{question} Расскажите подробнее."}
    )
    assert real.status_code == 200, real.text
    assert kronto.model.calls > calls


async def test_demo_reports_model_outage(kronto: Kronto) -> None:
    """ТЗ §1, §2: модель не отвечает — песочница честно сообщает об ошибке
    (в потоке — событие error), а не выдаёт пустой или выдуманный ответ."""
    await kronto.setup_demo()
    anonymous = kronto.browser()
    question = (await anonymous.get(f"{API}/demo")).json()["questions"][0][:250]
    kronto.model.unavailable = True

    stream = await anonymous.post(
        f"{API}/demo/ask/stream", json={"question": f"{question} Подробнее."}
    )
    assert stream.status_code == 200, stream.text
    events = sse_events(stream.text)
    types = [event.get("type") for event in events]
    assert "error" in types, stream.text[:500]
    assert "done" not in types
    errors = [event for event in events if event.get("type") == "error"]
    assert errors[0]["message"].strip()

    whole = await anonymous.post(
        f"{API}/demo/ask", json={"question": f"{question} Ещё подробнее."}
    )
    # допущение: целый ответ при упавшей модели — 503 demo_busy (не 200 и не 500)
    assert whole.status_code in (502, 503, 504), whole.text


# --------------------------------------------------------------------------
# §11. Согласие на обработку персональных данных
# --------------------------------------------------------------------------


async def test_registration_requires_consent(kronto: Kronto) -> None:
    """ТЗ §2, §11: регистрация — с согласием на обработку персональных
    данных; без него учётка не заводится и письмо не уходит."""
    email = f"nosign@{DOMAIN}"
    browser = kronto.browser()
    body = {
        "first_name": "Анна",
        "last_name": "Безсогласия",
        "email": email,
        "password": new_password(),
    }
    for consent in ({"consent": False}, {}):
        response = await browser.post(f"{API}/auth/register", json={**body, **consent})
        assert response.status_code == 422, f"{consent}: {response.status_code}"
    assert await kronto.inbox(email) == []

    response = await browser.post(
        f"{API}/auth/register", json={**body, "consent": True}
    )
    assert response.status_code == 202, response.text
    assert six_digit_code(await kronto.inbox(email)), (
        "с согласием письмо с кодом приходит"
    )
