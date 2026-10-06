"""Чёрный ящик: учётки, вход, второй фактор, безопасность учётки.

Написано только по ТЗ (§2, §3, §4, §9 в части входа команды) и схеме API,
без чтения кода продукта. Где ТЗ молчит о детали (точный код ошибки,
текст, механизм), проверяется то, что обязано выполняться при любом
разумном решении, с пометкой «# допущение: …».

Дисциплина лимитов частоты (HARNESS: лимиты как в бою, один IP на все
браузеры): в каждом тесте не больше 3–4 попыток входа, не больше двух
регистраций и одного запроса «Забыли пароль?».
"""

from __future__ import annotations

import html as html_lib
import inspect
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import httpx
import pytest

from tests.blackbox.conftest import API, Kronto

DOMAIN = "corp-test.ru"
PASSWORD = "Kr0nto-Nadezhnyi-Parol-2026!"
NEW_PASSWORD = "Novyi-Parol-Posle-Smeny-2026?"
OTHER_PASSWORD = "Chuzhoi-Parol-Zloumyshlennika-2026#"
WRONG_PASSWORD = "Nevernyi-Parol-Sovsem-2026!"
ADMIN_TEMP_PASSWORD = "Vremennyi-Parol-Admina-2026!"
# допущение: ТЗ правил пароля не задаёт (схема упоминает «политику»);
# пароль из 5 символов отвергает любая разумная политика.
WEAK_PASSWORD = "qwe12"


# ---------------------------------------------------------------- общие мелочи


def addr(name: str) -> str:
    return f"{name}@{DOMAIN}"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def show(r: httpx.Response) -> str:
    return f"{r.request.method} {r.request.url.path} -> {r.status_code}: {r.text[:500]}"


def expect(r: httpx.Response, status: int) -> Any:
    assert r.status_code == status, show(r)
    if r.status_code == 204 or not r.content:
        return None
    return r.json()


def assert_refused(r: httpx.Response) -> None:
    """Отказ: код 4xx и никакого токена в ответе."""
    assert 400 <= r.status_code < 500, "ожидался отказ 4xx; " + show(r)
    try:
        body = r.json()
    except ValueError:
        return
    if isinstance(body, dict):
        assert not body.get("access_token"), "в отказе есть токен; " + show(r)


def other_code(code: str) -> str:
    """Шесть цифр, заведомо не равные данному коду."""
    return f"{(int(code) + 1) % 1_000_000:06d}"


async def maybe_await(value: Any) -> Any:
    # HARNESS не уточняет, синхронны ли .register()/.sign() ключа доступа.
    if inspect.isawaitable(value):
        return await value
    return value


# ---------------------------------------------------------------- письма

_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-.~]{16,128}={0,2}")
_CODE_RE = re.compile(r"(?<![\w#&/=-])(\d{6})(?![\w%-])")
_SPLIT_CODE_RE = re.compile(r"(?<![\w#&/=-])(\d{3})[  -](\d{3})(?![\w%-])")
_STYLE_RE = re.compile(r"<(style|script)[^>]*>.*?</\1>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _letter_parts(letter: Any) -> tuple[str, str, str]:
    subject = getattr(letter, "subject", "") or ""
    text = getattr(letter, "text", "") or ""
    raw_html = getattr(letter, "html", "") or ""
    return subject, text, html_lib.unescape(raw_html)


def find_code(letter: Any) -> str | None:
    """Код из 6 цифр в письме (ТЗ §3). Ссылки вырезаются: в токенах бывают цифры."""
    subject, text, html_text = _letter_parts(letter)
    plain_html = _TAG_RE.sub(" ", _STYLE_RE.sub(" ", html_text))
    chunks = [_URL_RE.sub(" ", chunk) for chunk in (text, subject, plain_html)]
    for pattern in (_CODE_RE, _SPLIT_CODE_RE):
        for chunk in chunks:
            match = pattern.search(chunk)
            if match:
                return "".join(match.groups())
    return None


def _split_fragment(fragment: str) -> tuple[str, str]:
    if "?" in fragment:
        path, query = fragment.split("?", 1)
        return path, query
    if "=" in fragment:
        return "", fragment
    return fragment, ""


def _tokenish(value: str) -> bool:
    # Случайный токен почти наверняка содержит цифру или заглавную букву;
    # так отсеиваются слова пути вроде «security-settings».
    return bool(_TOKEN_RE.fullmatch(value)) and bool(re.search(r"[0-9A-Z]", value))


def link_tokens(letter: Any) -> list[str]:
    """Токены из ссылок письма: сначала параметр token, потом прочие кандидаты."""
    _, text, html_text = _letter_parts(letter)
    urls: list[str] = []
    for source in (text, html_text):
        for url in _URL_RE.findall(source):
            url = url.rstrip(".,;:!?»)")
            if url not in urls:
                urls.append(url)
    named: list[str] = []
    other: list[str] = []
    for url in urls:
        parts = urlsplit(url)
        frag_path, frag_query = _split_fragment(parts.fragment)
        for key, value in parse_qsl(parts.query) + parse_qsl(frag_query):
            if _tokenish(value):
                (named if key.lower() in {"token", "t", "key"} else other).append(value)
        for segment in f"{parts.path}/{frag_path}".split("/"):
            segment = unquote(segment)
            if _tokenish(segment):
                other.append(segment)
    result: list[str] = []
    for token in named + other:
        if token not in result:
            result.append(token)
    return result


async def count_letters(kronto: Kronto, email: str) -> int:
    return len(await kronto.inbox(email))


async def letters_since(kronto: Kronto, email: str, before: int) -> list[Any]:
    return list(await kronto.inbox(email))[before:]


async def code_since(kronto: Kronto, email: str, before: int) -> str:
    """Код из самого свежего письма с кодом, пришедшего после отметки before."""
    fresh = await letters_since(kronto, email, before)
    codes = [code for code in map(find_code, fresh) if code]
    assert codes, (
        f"на {email} не пришло письмо с кодом из 6 цифр (новых писем: {len(fresh)})"
    )
    return codes[-1]


async def link_since(kronto: Kronto, email: str, before: int) -> str:
    """Токен из ссылки самого свежего письма со ссылкой после отметки before."""
    fresh = await letters_since(kronto, email, before)
    for letter in reversed(fresh):
        tokens = link_tokens(letter)
        if tokens:
            return tokens[0]
    raise AssertionError(
        f"на {email} не пришло письмо со ссылкой (новых писем: {len(fresh)})"
    )


# ---------------------------------------------------------------- люди и вход


@dataclass
class User:
    email: str
    browser: httpx.AsyncClient
    token: str
    password: str = PASSWORD
    totp_secret: str | None = None
    tenant_id: str | None = None

    @property
    def auth(self) -> dict[str, str]:
        return bearer(self.token)


def restart_browser(kronto: Kronto, browser: httpx.AsyncClient) -> httpx.AsyncClient:
    """«Закрыть и снова открыть браузер»: сеансовые cookie (без срока) пропадают,
    постоянные остаются."""
    reopened = kronto.browser()
    for cookie in browser.cookies.jar:
        if cookie.expires is not None:
            reopened.cookies.jar.set_cookie(cookie)
    return reopened


async def get_me(browser: httpx.AsyncClient, token: str) -> httpx.Response:
    return await browser.get(f"{API}/auth/me", headers=bearer(token))


async def security(user: User) -> dict[str, Any]:
    return expect(
        await user.browser.get(f"{API}/account/security", headers=user.auth), 200
    )


async def refresh(browser: httpx.AsyncClient) -> httpx.Response:
    return await browser.post(f"{API}/auth/refresh")


async def register(
    browser: httpx.AsyncClient,
    email: str,
    password: str = PASSWORD,
    first_name: str = "Анна",
    last_name: str = "Петрова",
) -> httpx.Response:
    return await browser.post(
        f"{API}/auth/register",
        json={
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "password": password,
            "consent": True,
        },
    )


async def confirm_by_code(
    browser: httpx.AsyncClient, email: str, code: str
) -> httpx.Response:
    return await browser.post(
        f"{API}/auth/verify-email", json={"email": email, "code": code}
    )


async def register_and_confirm(
    kronto: Kronto,
    name: str,
    password: str = PASSWORD,
    first_name: str = "Анна",
    last_name: str = "Петрова",
) -> User:
    """Регистрация и подтверждение кодом из письма (вход сразу, ТЗ §3)."""
    email = addr(name)
    browser = kronto.browser()
    before = await count_letters(kronto, email)
    expect(await register(browser, email, password, first_name, last_name), 202)
    code = await code_since(kronto, email, before)
    body = expect(await confirm_by_code(browser, email, code), 200)
    return User(
        email=email, browser=browser, token=body["access_token"], password=password
    )


async def login(
    browser: httpx.AsyncClient,
    email: str,
    password: str = PASSWORD,
    remember: bool = True,
) -> httpx.Response:
    return await browser.post(
        f"{API}/auth/login",
        json={"email": email, "password": password, "remember": remember},
    )


async def start_login(
    browser: httpx.AsyncClient,
    email: str,
    password: str = PASSWORD,
    remember: bool = True,
) -> dict[str, Any]:
    """Первый шаг входа на новом устройстве: пароль принят, нужен второй фактор."""
    body = expect(await login(browser, email, password, remember), 200)
    assert body["status"] == "mfa_required", body
    assert not body.get("access_token"), body
    assert body["mfa"] and len(body["mfa"]["token"]) >= 16, body
    return body["mfa"]


async def verify(
    browser: httpx.AsyncClient,
    token: str,
    method: str,
    code: str | None = None,
    credential: dict[str, Any] | None = None,
) -> httpx.Response:
    payload: dict[str, Any] = {"token": token, "method": method}
    if code is not None:
        payload["code"] = code
    if credential is not None:
        payload["credential"] = credential
    return await browser.post(f"{API}/auth/mfa/verify", json=payload)


async def login_by_email_code(
    kronto: Kronto,
    email: str,
    password: str = PASSWORD,
    remember: bool = True,
    browser: httpx.AsyncClient | None = None,
) -> User:
    browser = browser or kronto.browser()
    before = await count_letters(kronto, email)
    mfa = await start_login(browser, email, password, remember)
    assert "email" in mfa["methods"], mfa
    code = await code_since(kronto, email, before)
    body = expect(await verify(browser, mfa["token"], "email", code=code), 200)
    return User(
        email=email, browser=browser, token=body["access_token"], password=password
    )


async def login_by_totp(
    kronto: Kronto,
    email: str,
    secret: str,
    password: str = PASSWORD,
    remember: bool = True,
    browser: httpx.AsyncClient | None = None,
) -> User:
    browser = browser or kronto.browser()
    mfa = await start_login(browser, email, password, remember)
    assert "totp" in mfa["methods"], mfa
    body = expect(
        await verify(browser, mfa["token"], "totp", code=kronto.totp(secret)), 200
    )
    return User(
        email=email,
        browser=browser,
        token=body["access_token"],
        password=password,
        totp_secret=secret,
    )


async def enable_totp_in_settings(kronto: Kronto, user: User) -> list[str]:
    """Приложение-аутентификатор через «Настройки → Безопасность»; резервные коды."""
    setup = expect(
        await user.browser.post(f"{API}/account/totp/setup", headers=user.auth), 200
    )
    body = expect(
        await user.browser.post(
            f"{API}/account/totp/enable",
            headers=user.auth,
            json={
                "setup_token": setup["setup_token"],
                "code": kronto.totp(setup["secret"]),
            },
        ),
        200,
    )
    user.totp_secret = setup["secret"]
    return list(body["backup_codes"] or [])


async def add_passkey(
    kronto: Kronto, user: User, name: str = "Ноутбук"
) -> tuple[Any, dict[str, Any]]:
    key = kronto.passkey()
    setup = expect(
        await user.browser.post(f"{API}/account/passkeys/options", headers=user.auth),
        200,
    )
    credential = await maybe_await(key.register(setup["options"]))
    created = expect(
        await user.browser.post(
            f"{API}/account/passkeys",
            headers=user.auth,
            json={
                "setup_token": setup["setup_token"],
                "credential": credential,
                "name": name,
            },
        ),
        201,
    )
    return key, created


async def passkey_options(browser: httpx.AsyncClient, mfa_token: str) -> dict[str, Any]:
    body = expect(
        await browser.post(
            f"{API}/auth/mfa/passkey-options", json={"token": mfa_token}
        ),
        200,
    )
    return body["options"]


async def settle_admin(admin: User) -> None:
    """Временный пароль сменить, компанию выбрать — чтобы тест проверял своё."""
    profile = expect(await get_me(admin.browser, admin.token), 200)
    if profile["must_change_password"]:
        body = expect(
            await admin.browser.post(
                f"{API}/auth/change-password",
                headers=admin.auth,
                json={"current_password": admin.password, "new_password": PASSWORD},
            ),
            200,
        )
        admin.token = body["access_token"]
        admin.password = PASSWORD
        profile = expect(await get_me(admin.browser, admin.token), 200)
    current = profile["company"]
    if admin.tenant_id and (
        current is None or str(current["tenant_id"]) != admin.tenant_id
    ):
        body = expect(
            await admin.browser.post(
                f"{API}/auth/switch-company",
                headers=admin.auth,
                json={"tenant_id": admin.tenant_id},
            ),
            200,
        )
        admin.token = body["access_token"]


async def first_company_id(user: User) -> str | None:
    """Текущая или первая компания человека из /auth/me."""
    profile = expect(await get_me(user.browser, user.token), 200)
    if profile["company"]:
        return str(profile["company"]["tenant_id"])
    if profile["companies"]:
        return str(profile["companies"][0]["tenant_id"])
    return None


async def session_without_strong_factor(
    kronto: Kronto, browser: httpx.AsyncClient, email: str, password: str
) -> str | None:
    """Попробовать войти без приложения и ключа — кодом на почту.

    Возвращает токен, если пустили, или None, если система не пустила (оба
    варианта разумны для тех, кому обязателен надёжный фактор). Сразу выданный
    сеанс на новом устройстве без второго фактора — нарушение ТЗ §3.
    """
    before = await count_letters(kronto, email)
    first = await login(browser, email, password)
    if first.status_code != 200:
        assert_refused(first)
        return None
    body = first.json()
    assert body["status"] == "mfa_required", body
    if "email" not in body["mfa"]["methods"]:
        return None
    code = await code_since(kronto, email, before)
    r = await verify(browser, body["mfa"]["token"], "email", code=code)
    if r.status_code != 200:
        assert_refused(r)
        return None
    return r.json()["access_token"]


async def make_admin(kronto: Kronto, code: str = "qa", name: str = "QA") -> User:
    """Компания от команды kronto и её администратор с приложением-аутентификатором."""
    email = addr(f"admin-{code}")
    company = await kronto.create_company(
        code=code, name=name, admin_email=email, admin_password=ADMIN_TEMP_PASSWORD
    )
    secret = await kronto.enable_totp(email)
    admin = await login_by_totp(kronto, email, secret, password=ADMIN_TEMP_PASSWORD)
    admin.tenant_id = str(company["id"])
    await settle_admin(admin)
    return admin


async def join_company(admin: User, user: User) -> None:
    """Вступление по приглашению (ТЗ §2): ссылку создаёт админ."""
    invite = expect(
        await admin.browser.post(f"{API}/invites", headers=admin.auth, json={}), 201
    )
    body = expect(
        await user.browser.post(
            f"{API}/invites/accept", headers=user.auth, json={"secret": invite["token"]}
        ),
        200,
    )
    assert body["outcome"] == "joined", body
    if body["session"]:
        user.token = body["session"]["access_token"]
    user.tenant_id = admin.tenant_id


async def make_employee(kronto: Kronto, admin: User, name: str) -> User:
    user = await register_and_confirm(
        kronto, name, first_name="Сергей", last_name="Сотрудников"
    )
    await join_company(admin, user)
    return user


async def list_sessions(user: User) -> list[dict[str, Any]]:
    return expect(
        await user.browser.get(f"{API}/auth/sessions", headers=user.auth), 200
    )


async def current_session_id(user: User) -> str:
    items = await list_sessions(user)
    current = [item for item in items if item["current"]]
    assert len(current) == 1, items
    return str(current[0]["id"])


async def request_email_change(
    user: User, new_email: str, code: str | None = None, password: str | None = None
) -> httpx.Response:
    payload: dict[str, Any] = {
        "new_email": new_email,
        "password": password or user.password,
    }
    if code is not None:
        payload["code"] = code
    return await user.browser.post(
        f"{API}/account/email", headers=user.auth, json=payload
    )


async def change_email_until_link(kronto: Kronto, user: User, new_email: str) -> str:
    """Смена почты без приложения: код на прежний адрес → ссылка на новый."""
    before_old = await count_letters(kronto, user.email)
    before_new = await count_letters(kronto, new_email)
    first = expect(await request_email_change(user, new_email), 202)
    assert first["status"] == "code_sent", first
    code = await code_since(kronto, user.email, before_old)
    second = expect(await request_email_change(user, new_email, code=code), 202)
    assert second["status"] == "link_sent", second
    return await link_since(kronto, new_email, before_new)


# === §2–3: регистрация и подтверждение почты


async def test_register_letter_has_code_and_link_and_code_logs_in(
    kronto: Kronto,
) -> None:
    """ТЗ §2, §3: регистрация (имя, фамилия, почта, пароль, согласие) → письмо с
    кодом из 6 цифр и ссылкой; код подтверждает почту и сразу впускает.
    Учётка не зависит от компании: после регистрации компаний нет. Код
    одноразовый."""
    email = addr("anna")
    browser = kronto.browser()
    body = expect(await register(browser, email), 202)
    assert body["email"] == email

    letters = await kronto.inbox(email)
    both = [letter for letter in letters if find_code(letter) and link_tokens(letter)]
    assert both, "письмо о подтверждении должно содержать и код из 6 цифр, и ссылку"
    code = find_code(both[-1])
    assert code is not None

    session = expect(await confirm_by_code(browser, email, code), 200)
    assert session["access_token"]
    profile = expect(await get_me(browser, session["access_token"]), 200)
    assert profile["email"] == email
    assert profile["first_name"] == "Анна"
    assert profile["last_name"] == "Петрова"
    assert profile["company"] is None
    assert profile["companies"] == []
    assert profile["must_change_password"] is False

    # Тот же код второй раз — уже не вход (иначе код работал бы как пароль).
    assert_refused(await confirm_by_code(kronto.browser(), email, code))


async def test_confirm_email_by_link(kronto: Kronto) -> None:
    """ТЗ §3: почту подтверждает и ссылка из письма (можно открыть на другом
    устройстве); выдуманный токен ссылки не подтверждает."""
    email = addr("boris")
    expect(
        await register(kronto.browser(), email, first_name="Борис", last_name="Иванов"),
        202,
    )
    token = await link_since(kronto, email, 0)

    phone = kronto.browser()
    assert_refused(
        await phone.post(
            f"{API}/auth/verify-email/link", json={"token": "Q1" + "x" * 30}
        )
    )

    session = expect(
        await phone.post(f"{API}/auth/verify-email/link", json={"token": token}), 200
    )
    profile = expect(await get_me(phone, session["access_token"]), 200)
    assert profile["email"] == email
    assert profile["first_name"] == "Борис"


async def test_login_refused_until_email_confirmed(kronto: Kronto) -> None:
    """ТЗ §3: «без подтверждения не войти»; после подтверждения вход идёт."""
    email = addr("vera")
    expect(await register(kronto.browser(), email), 202)

    # допущение: отказ — код 4xx (ТЗ кода не задаёт); ни токена, ни сеанса.
    assert_refused(await login(kronto.browser(), email))

    code = await code_since(kronto, email, 0)
    expect(await confirm_by_code(kronto.browser(), email, code), 200)
    body = expect(await login(kronto.browser(), email), 200)
    assert body["status"] in ("ok", "mfa_required"), body


async def test_wrong_confirmation_code_refused(kronto: Kronto) -> None:
    """ТЗ §3: почту подтверждает только верный код из 6 цифр."""
    email = addr("galina")
    browser = kronto.browser()
    expect(await register(browser, email), 202)
    code = await code_since(kronto, email, 0)

    assert_refused(await confirm_by_code(browser, email, other_code(code)))
    # Не 6 цифр — ошибка ввода по схеме.
    malformed = await confirm_by_code(browser, email, "12345")
    assert malformed.status_code == 422, show(malformed)

    # допущение: одна ошибка не сжигает код (разумный лимит попыток больше одной).
    expect(await confirm_by_code(browser, email, code), 200)


async def test_someone_elses_confirmation_code_refused(kronto: Kronto) -> None:
    """ТЗ §3: код подтверждения привязан к своей почте — чужой код не подходит."""
    email_a, email_b = addr("dina"), addr("egor")
    expect(await register(kronto.browser(), email_a), 202)
    expect(
        await register(
            kronto.browser(), email_b, first_name="Егор", last_name="Смирнов"
        ),
        202,
    )
    code_a = await code_since(kronto, email_a, 0)
    code_b = await code_since(kronto, email_b, 0)

    browser = kronto.browser()
    if code_b != code_a:
        assert_refused(await confirm_by_code(browser, email_a, code_b))
    session = expect(await confirm_by_code(browser, email_a, code_a), 200)
    profile = expect(await get_me(browser, session["access_token"]), 200)
    assert profile["email"] == email_a


_DROP = object()


@pytest.mark.parametrize(
    "patch",
    [
        pytest.param({"consent": _DROP}, id="no-consent"),
        pytest.param({"consent": False}, id="consent-false"),
        pytest.param({"first_name": ""}, id="empty-first-name"),
        pytest.param({"last_name": ""}, id="empty-last-name"),
        pytest.param({"first_name": _DROP}, id="no-first-name"),
        pytest.param({"last_name": "Я" * 101}, id="last-name-over-100"),
        pytest.param({"email": "not-an-email"}, id="not-an-email"),
        pytest.param({"password": "Aa1-" + "x" * 125}, id="password-over-128"),
        pytest.param({"company": "qa"}, id="company-code-field"),
    ],
)
async def test_register_rejects_invalid_input(
    kronto: Kronto, patch: dict[str, Any]
) -> None:
    """ТЗ §2: регистрация — имя, фамилия, почта, пароль и согласие на обработку
    ПДн, без кода компании; без любого из них — ошибка ввода, письма нет."""
    email = addr("invalid")
    payload: dict[str, Any] = {
        "first_name": "Анна",
        "last_name": "Петрова",
        "email": email,
        "password": PASSWORD,
        "consent": True,
    }
    for key, value in patch.items():
        if value is _DROP:
            payload.pop(key)
        else:
            payload[key] = value

    r = await kronto.browser().post(f"{API}/auth/register", json=payload)
    assert r.status_code == 422, show(r)
    if payload.get("email") == email:
        assert await count_letters(kronto, email) == 0


async def test_register_rejects_weak_password(kronto: Kronto) -> None:
    """ТЗ §2: регистрация с паролем; заведомо слабый пароль не принимается."""
    email = addr("zhanna")
    # допущение: см. WEAK_PASSWORD — ТЗ правил не задаёт, 5 символов не пропустит никто.
    assert_refused(await register(kronto.browser(), email, password=WEAK_PASSWORD))
    assert await count_letters(kronto, email) == 0


async def test_duplicate_registration_same_answer_no_takeover(kronto: Kronto) -> None:
    """ТЗ §2: учётка одна на человека и почту. Повторная регистрация на занятую
    почту отвечает так же, как первая (схема: «по ответу не перебрать адреса»),
    и не меняет ни пароль, ни имя владельца."""
    email = addr("zoya")
    owner_browser = kronto.browser()
    first = await register(owner_browser, email, first_name="Зоя", last_name="Орлова")
    expect(first, 202)
    code = await code_since(kronto, email, 0)
    owner_session = expect(await confirm_by_code(owner_browser, email, code), 200)

    dup = await register(
        kronto.browser(),
        email,
        password=OTHER_PASSWORD,
        first_name="Злоумышленник",
        last_name="Чужой",
    )
    # допущение: «ответ одинаковый» — тот же код и то же тело.
    assert dup.status_code == first.status_code == 202, show(dup)
    assert dup.json() == first.json()

    assert_refused(await login(kronto.browser(), email, OTHER_PASSWORD))
    body = expect(await login(kronto.browser(), email, PASSWORD), 200)
    assert body["status"] in ("ok", "mfa_required"), body
    profile = expect(await get_me(owner_browser, owner_session["access_token"]), 200)
    assert profile["first_name"] == "Зоя"
    assert profile["last_name"] == "Орлова"


async def test_resend_verification_same_answer_and_latest_code_works(
    kronto: Kronto,
) -> None:
    """ТЗ §3: подтверждение почты кодом; повторная отправка письма не выдаёт,
    есть ли учётка (схема), и последний код подтверждает почту."""
    email, nobody = addr("igor"), addr("nobody")
    browser = kronto.browser()
    expect(await register(browser, email), 202)

    known = await browser.post(f"{API}/auth/verify-email/resend", json={"email": email})
    unknown = await browser.post(
        f"{API}/auth/verify-email/resend", json={"email": nobody}
    )
    assert unknown.status_code == 202, show(unknown)
    assert unknown.json()["email"] == nobody
    # допущение: повтор сразу после первого письма может упереться в паузу (429).
    assert known.status_code in (202, 429), show(known)
    if known.status_code == 202:
        assert set(known.json()) == set(unknown.json())
    assert await count_letters(kronto, nobody) == 0

    code = await code_since(kronto, email, 0)
    expect(await confirm_by_code(browser, email, code), 200)


# === §3: вход и второй фактор


async def test_new_device_login_requires_email_code(kronto: Kronto) -> None:
    """ТЗ §3: вход — почта + пароль + второй фактор; по умолчанию — код на почту
    при входе с нового устройства, ничего настраивать не нужно. Шаг входа
    одноразовый."""
    user = await register_and_confirm(kronto, "kira")
    browser = kronto.browser()
    before = await count_letters(kronto, user.email)

    body = expect(await login(browser, user.email), 200)
    assert body["status"] == "mfa_required", body
    assert not body.get("access_token")
    assert "email" in body["mfa"]["methods"]

    code = await code_since(kronto, user.email, before)
    session = expect(
        await verify(browser, body["mfa"]["token"], "email", code=code), 200
    )
    profile = expect(await get_me(browser, session["access_token"]), 200)
    assert profile["email"] == user.email

    assert_refused(
        await verify(kronto.browser(), body["mfa"]["token"], "email", code=code)
    )


async def test_wrong_password_and_unknown_email_refused_alike_without_code(
    kronto: Kronto,
) -> None:
    """ТЗ §3: вход — почта + пароль. Неверный пароль — отказ, код на почту не
    уходит; отказ для несуществующей почты такой же (не выдаёт учётку)."""
    user = await register_and_confirm(kronto, "lev")
    nobody = addr("nobody")
    before = await count_letters(kronto, user.email)

    wrong = await login(kronto.browser(), user.email, WRONG_PASSWORD)
    unknown = await login(kronto.browser(), nobody, WRONG_PASSWORD)
    assert_refused(wrong)
    assert_refused(unknown)
    # допущение: «не выдавать учётку» — одинаковые код и тело отказа.
    assert wrong.status_code == unknown.status_code
    assert wrong.text == unknown.text

    assert await count_letters(kronto, user.email) == before
    assert await count_letters(kronto, nobody) == 0


async def test_wrong_or_unavailable_second_factor_refused(kronto: Kronto) -> None:
    """ТЗ §3: второй фактор — только верный код; способ, который у учётки не
    включён (приложение), не принимается. Верный код после ошибки работает."""
    user = await register_and_confirm(kronto, "maya")
    browser = kronto.browser()
    before = await count_letters(kronto, user.email)
    mfa = await start_login(browser, user.email)
    code = await code_since(kronto, user.email, before)

    assert_refused(await verify(browser, mfa["token"], "email", code=other_code(code)))
    assert_refused(await verify(browser, mfa["token"], "totp", code="123456"))
    # допущение: две ошибки не сжигают шаг входа.
    session = expect(await verify(browser, mfa["token"], "email", code=code), 200)
    assert session["access_token"]


async def test_login_challenge_bound_to_its_account(kronto: Kronto) -> None:
    """ТЗ §3: код второго фактора — для своего входа; код другого человека к
    чужому шагу входа не подходит."""
    first = await register_and_confirm(kronto, "nina")
    second = await register_and_confirm(
        kronto, "oleg", first_name="Олег", last_name="Сидоров"
    )
    browser_a, browser_b = kronto.browser(), kronto.browser()
    before_a = await count_letters(kronto, first.email)
    before_b = await count_letters(kronto, second.email)
    mfa_a = await start_login(browser_a, first.email)
    await start_login(browser_b, second.email)
    code_a = await code_since(kronto, first.email, before_a)
    code_b = await code_since(kronto, second.email, before_b)

    if code_b != code_a:
        assert_refused(await verify(browser_a, mfa_a["token"], "email", code=code_b))
    session = expect(await verify(browser_a, mfa_a["token"], "email", code=code_a), 200)
    profile = expect(await get_me(browser_a, session["access_token"]), 200)
    assert profile["email"] == first.email


async def test_resend_login_code_latest_code_works(kronto: Kronto) -> None:
    """ТЗ §3: код на почту при входе; запрос нового кода не ломает вход —
    последний пришедший код впускает."""
    user = await register_and_confirm(kronto, "pavel")
    browser = kronto.browser()
    before = await count_letters(kronto, user.email)
    mfa = await start_login(browser, user.email)

    r = await browser.post(f"{API}/auth/mfa/resend", json={"token": mfa["token"]})
    # допущение: повтор сразу после первого письма может быть придержан паузой (429).
    assert r.status_code in (202, 429), show(r)

    code = await code_since(kronto, user.email, before)
    expect(await verify(browser, mfa["token"], "email", code=code), 200)


async def test_app_login_without_email_fallback(kronto: Kronto) -> None:
    """ТЗ §3: приложение-аутентификатор — надёжный второй фактор. При включённом
    приложении сброс пароля по почте требует и его — значит, и вход одним кодом
    с почты больше не проходит."""
    user = await register_and_confirm(
        kronto, "rodion", first_name="Родион", last_name="Гусев"
    )
    secret = await kronto.enable_totp(user.email)
    browser = kronto.browser()

    mfa = await start_login(browser, user.email)
    assert "totp" in mfa["methods"], mfa
    # допущение: вывод из правила сброса пароля (ТЗ §3) — почта не заменяет приложение.
    assert "email" not in mfa["methods"], mfa
    assert_refused(await verify(browser, mfa["token"], "email", code="123456"))

    session = expect(
        await verify(browser, mfa["token"], "totp", code=kronto.totp(secret)), 200
    )
    profile = expect(await get_me(browser, session["access_token"]), 200)
    assert profile["email"] == user.email


async def test_new_device_login_sends_security_letter(kronto: Kronto) -> None:
    """ТЗ §3: письма о безопасности — вход с нового устройства. С приложением код
    на почту не нужен, поэтому любое новое письмо после входа — уведомление."""
    user = await register_and_confirm(kronto, "rita")
    secret = await kronto.enable_totp(user.email)
    before = await count_letters(kronto, user.email)

    await login_by_totp(kronto, user.email, secret)

    fresh = await letters_since(kronto, user.email, before)
    assert fresh, "после входа с нового устройства должно прийти письмо о безопасности"
    for letter in fresh:
        assert PASSWORD not in (getattr(letter, "text", "") or "")


async def test_backup_code_login_is_single_use(kronto: Kronto) -> None:
    """ТЗ §3: 10 резервных кодов; резервный код впускает вместо приложения,
    каждый — один раз."""
    user = await register_and_confirm(
        kronto, "semen", first_name="Семён", last_name="Белов"
    )
    codes = await enable_totp_in_settings(kronto, user)
    assert len(codes) == 10

    first = kronto.browser()
    mfa = await start_login(first, user.email)
    assert "backup" in mfa["methods"], mfa
    session = expect(await verify(first, mfa["token"], "backup", code=codes[0]), 200)
    state = expect(
        await first.get(
            f"{API}/account/security", headers=bearer(session["access_token"])
        ),
        200,
    )
    # допущение: использованный код вычитается из оставшихся.
    assert state["backup_codes_left"] == 9

    second = kronto.browser()
    mfa = await start_login(second, user.email)
    assert_refused(await verify(second, mfa["token"], "backup", code=codes[0]))
    expect(await verify(second, mfa["token"], "backup", code=codes[1]), 200)


async def test_passkey_register_and_login(kronto: Kronto) -> None:
    """ТЗ §3, §4: ключ доступа подключается в «Настройки → Безопасность» и служит
    вторым фактором при входе; письмо о смене второго фактора."""
    user = await register_and_confirm(
        kronto, "taras", first_name="Тарас", last_name="Ковалёв"
    )
    before = await count_letters(kronto, user.email)
    key, created = await add_passkey(kronto, user, "Ноутбук")
    assert created["passkey"]["name"] == "Ноутбук"
    if created["backup_codes"] is not None:
        assert len(created["backup_codes"]) == 10
    state = await security(user)
    assert [str(p["id"]) for p in state["passkeys"]] == [str(created["passkey"]["id"])]
    assert await count_letters(kronto, user.email) > before

    browser = kronto.browser()
    mfa = await start_login(browser, user.email)
    assert "passkey" in mfa["methods"], mfa
    options = await passkey_options(browser, mfa["token"])
    credential = await maybe_await(key.sign(options))
    session = expect(
        await verify(browser, mfa["token"], "passkey", credential=credential), 200
    )
    profile = expect(await get_me(browser, session["access_token"]), 200)
    assert profile["email"] == user.email


async def test_foreign_passkey_refused(kronto: Kronto) -> None:
    """ТЗ §3: при входе принимается только ключ доступа этой учётки; ключ другого
    человека — отказ."""
    owner = await register_and_confirm(
        kronto, "uliana", first_name="Ульяна", last_name="Ершова"
    )
    stranger = await register_and_confirm(
        kronto, "fedor", first_name="Фёдор", last_name="Жуков"
    )
    own_key, _ = await add_passkey(kronto, owner)
    stranger_key, _ = await add_passkey(kronto, stranger)

    browser = kronto.browser()
    mfa = await start_login(browser, owner.email)
    foreign = await maybe_await(
        stranger_key.sign(await passkey_options(browser, mfa["token"]))
    )
    assert_refused(await verify(browser, mfa["token"], "passkey", credential=foreign))

    own = await maybe_await(own_key.sign(await passkey_options(browser, mfa["token"])))
    expect(await verify(browser, mfa["token"], "passkey", credential=own), 200)


async def test_passkey_delete_requires_own_password(kronto: Kronto) -> None:
    """ТЗ §4: ключи доступа в настройках безопасности; удалить ключ может только
    сам человек, подтвердив паролем; письмо о смене второго фактора."""
    owner = await register_and_confirm(
        kronto, "khariton", first_name="Харитон", last_name="Лосев"
    )
    stranger = await register_and_confirm(
        kronto, "tsvetana", first_name="Цветана", last_name="Мухина"
    )
    _, created = await add_passkey(kronto, owner)
    url = f"{API}/account/passkeys/{created['passkey']['id']}/delete"

    assert_refused(
        await stranger.browser.post(
            url, headers=stranger.auth, json={"password": PASSWORD}
        )
    )
    assert_refused(
        await owner.browser.post(
            url, headers=owner.auth, json={"password": WRONG_PASSWORD}
        )
    )
    assert len((await security(owner))["passkeys"]) == 1

    before = await count_letters(kronto, owner.email)
    expect(
        await owner.browser.post(
            url, headers=owner.auth, json={"password": owner.password}
        ),
        204,
    )
    assert (await security(owner))["passkeys"] == []
    assert await count_letters(kronto, owner.email) > before


# === §3: «Запомнить это устройство»


async def test_remembered_device_skips_second_factor(kronto: Kronto) -> None:
    """ТЗ §3: «Запомнить это устройство» — без второго фактора на этом устройстве;
    другое устройство по-прежнему спрашивает второй фактор."""
    user = await register_and_confirm(kronto, "yana")
    browser = kronto.browser()
    await login_by_email_code(kronto, user.email, remember=True, browser=browser)

    again = expect(await login(browser, user.email, remember=True), 200)
    assert again["status"] == "ok", again
    assert again["access_token"]
    expect(await get_me(browser, again["access_token"]), 200)

    elsewhere = expect(await login(kronto.browser(), user.email, remember=True), 200)
    assert elsewhere["status"] == "mfa_required", elsewhere


async def test_without_remember_second_factor_asked_again(kronto: Kronto) -> None:
    """ТЗ §3: без галочки (чужой компьютер) устройство не запоминается — следующий
    вход снова со вторым фактором."""
    user = await register_and_confirm(kronto, "yulia")
    browser = kronto.browser()
    await login_by_email_code(kronto, user.email, remember=False, browser=browser)

    again = expect(await login(browser, user.email, remember=False), 200)
    # допущение: «чужой компьютер» не становится доверенным устройством.
    assert again["status"] == "mfa_required", again


async def test_session_survives_browser_restart_only_when_remembered(
    kronto: Kronto,
) -> None:
    """ТЗ §3: с галочкой — 30 дней без повторного входа; без галочки — сеанс до
    закрытия браузера."""
    user = await register_and_confirm(kronto, "eva")
    remembered = await login_by_email_code(kronto, user.email, remember=True)
    not_remembered = await login_by_email_code(kronto, user.email, remember=False)

    # Пока браузер открыт, сеанс жив в обоих случаях.
    expect(await refresh(not_remembered.browser), 200)

    # допущение: «закрыть браузер» = потерять сеансовые cookie, постоянные остаются.
    reopened = restart_browser(kronto, remembered.browser)
    body = expect(await refresh(reopened), 200)
    expect(await get_me(reopened, body["access_token"]), 200)

    reopened_public = restart_browser(kronto, not_remembered.browser)
    assert_refused(await refresh(reopened_public))


async def test_company_can_forbid_remember_device(kronto: Kronto) -> None:
    """ТЗ §3: админ компании может запретить галочку «Запомнить»; ТЗ §7: правило
    в настройках компании. Тогда устройство сотрудника не запоминается."""
    admin = await make_admin(kronto)
    employee = await make_employee(kronto, admin, "sotrudnik")

    settings = expect(
        await admin.browser.get(f"{API}/company", headers=admin.auth), 200
    )
    assert settings["allow_remember_device"] is True
    updated = expect(
        await admin.browser.patch(
            f"{API}/company", headers=admin.auth, json={"allow_remember_device": False}
        ),
        200,
    )
    assert updated["allow_remember_device"] is False

    browser = kronto.browser()
    await login_by_email_code(kronto, employee.email, remember=True, browser=browser)
    again = expect(await login(browser, employee.email, remember=True), 200)
    assert again["status"] == "mfa_required", again


# === §3, §4: сеансы и выход


async def test_sessions_list_and_end_other_device(kronto: Kronto) -> None:
    """ТЗ §3, §4: «Активные сеансы» — список устройств; завершённый сеанс другого
    устройства перестаёт действовать сразу, текущий — работает."""
    first = await register_and_confirm(kronto, "artem")
    second = await login_by_email_code(kronto, first.email)

    items = await list_sessions(second)
    assert len(items) >= 2, items
    assert sum(1 for item in items if item["current"]) == 1
    first_id = await current_session_id(first)
    second_id = await current_session_id(second)
    assert first_id != second_id
    assert {first_id, second_id} <= {str(item["id"]) for item in items}

    expect(
        await second.browser.post(
            f"{API}/auth/sessions/{first_id}/end", headers=second.auth
        ),
        204,
    )
    assert_refused(await get_me(first.browser, first.token))
    assert_refused(await refresh(first.browser))
    expect(await get_me(second.browser, second.token), 200)
    assert first_id not in {str(item["id"]) for item in await list_sessions(second)}


async def test_cannot_end_someone_elses_session(kronto: Kronto) -> None:
    """ТЗ §3: сеансы — свои; чужой сеанс не виден и не завершается."""
    victim = await register_and_confirm(
        kronto, "viktor", first_name="Виктор", last_name="Носов"
    )
    attacker = await register_and_confirm(
        kronto, "mallory", first_name="Маргарита", last_name="Злая"
    )
    victim_id = await current_session_id(victim)
    assert victim_id not in {str(item["id"]) for item in await list_sessions(attacker)}

    r = await attacker.browser.post(
        f"{API}/auth/sessions/{victim_id}/end", headers=attacker.auth
    )
    # допущение: точный код не задан (404 или 403), но это отказ.
    assert 400 <= r.status_code < 500, show(r)
    expect(await get_me(victim.browser, victim.token), 200)
    expect(await refresh(victim.browser), 200)


async def test_logout_ends_only_current_session(kronto: Kronto) -> None:
    """ТЗ §3: «выйти здесь» — закрывается этот сеанс, другие устройства остаются."""
    first = await register_and_confirm(kronto, "boris-l")
    second = await login_by_email_code(kronto, first.email)

    expect(await first.browser.post(f"{API}/auth/logout", headers=first.auth), 204)
    # допущение: как при завершении из списка, токены закрытого сеанса
    # не действуют сразу.
    assert_refused(await get_me(first.browser, first.token))
    assert_refused(await refresh(first.browser))
    expect(await get_me(second.browser, second.token), 200)


async def test_logout_everywhere(kronto: Kronto) -> None:
    """ТЗ §3: «выйти везде» — закрываются все сеансы, включая текущий."""
    first = await register_and_confirm(kronto, "vlad")
    second = await login_by_email_code(kronto, first.email)

    expect(
        await second.browser.post(f"{API}/auth/logout-all", headers=second.auth), 204
    )
    for user in (first, second):
        assert_refused(await get_me(user.browser, user.token))
        assert_refused(await refresh(user.browser))


async def test_refresh_gives_working_token_and_needs_session_cookie(
    kronto: Kronto,
) -> None:
    """ТЗ §3: сеанс продолжается без повторного входа (обновление токена из
    cookie); без cookie сеанса — отказ."""
    user = await register_and_confirm(kronto, "gleb")
    body = expect(await refresh(user.browser), 200)
    assert body["access_token"]
    profile = expect(await get_me(user.browser, body["access_token"]), 200)
    assert profile["email"] == user.email

    assert_refused(await refresh(kronto.browser()))


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/auth/me"),
        ("GET", "/auth/sessions"),
        ("POST", "/auth/logout-all"),
        ("GET", "/account/security"),
        ("POST", "/account/totp/setup"),
        ("POST", "/account/passkeys/options"),
        ("GET", "/account/company-requests"),
    ],
    ids=lambda value: str(value).strip("/").replace("/", "-"),
)
async def test_account_endpoints_require_login(
    kronto: Kronto, method: str, path: str
) -> None:
    """ТЗ §3, §4: настройки учётки и сеансы — только после входа; выдуманный
    токен не принимается."""
    browser = kronto.browser()
    anonymous = await browser.request(method, f"{API}{path}")
    assert anonymous.status_code in (401, 403), show(anonymous)
    forged = await browser.request(
        method, f"{API}{path}", headers=bearer("not-a-real-token")
    )
    assert forged.status_code in (401, 403), show(forged)


# === §3: пароль


async def test_forgot_password_same_answer_and_letter_only_for_real_account(
    kronto: Kronto,
) -> None:
    """ТЗ §3: восстановление пароля — ссылка в письме. Ответ не выдаёт, есть ли
    учётка (схема API); письмо получает только настоящая учётка."""
    user = await register_and_confirm(kronto, "darya")
    nobody = addr("nobody")
    before = await count_letters(kronto, user.email)

    known = await kronto.browser().post(
        f"{API}/auth/forgot-password", json={"email": user.email}
    )
    unknown = await kronto.browser().post(
        f"{API}/auth/forgot-password", json={"email": nobody}
    )
    assert known.status_code == unknown.status_code == 202, (
        show(known) + " | " + show(unknown)
    )
    assert set(known.json()) == set(unknown.json())
    assert unknown.json()["email"] == nobody

    assert await count_letters(kronto, nobody) == 0
    token = await link_since(kronto, user.email, before)
    assert len(token) >= 16


async def test_password_reset_by_link(kronto: Kronto) -> None:
    """ТЗ §3: восстановление пароля по ссылке из письма; ссылка одноразовая;
    после смены — выход на всех устройствах; письмо о смене пароля."""
    user = await register_and_confirm(kronto, "elena")
    before = await count_letters(kronto, user.email)
    expect(
        await kronto.browser().post(
            f"{API}/auth/forgot-password", json={"email": user.email}
        ),
        202,
    )
    token = await link_since(kronto, user.email, before)
    page = kronto.browser()
    url = f"{API}/auth/reset-password"

    # допущение: слабый пароль отвергается, а ссылка при этом не сгорает.
    assert_refused(
        await page.post(url, json={"token": token, "new_password": WEAK_PASSWORD})
    )

    before_change = await count_letters(kronto, user.email)
    body = expect(
        await page.post(url, json={"token": token, "new_password": NEW_PASSWORD}), 200
    )
    expect(await get_me(page, body["access_token"]), 200)

    # Выход на всех устройствах.
    assert_refused(await get_me(user.browser, user.token))
    assert_refused(await refresh(user.browser))
    # Письмо о смене пароля.
    assert await count_letters(kronto, user.email) > before_change
    # Ссылка одноразовая.
    assert_refused(
        await kronto.browser().post(
            url, json={"token": token, "new_password": "Esche-Odin-Parol-2026!"}
        )
    )
    # Старый пароль больше не подходит, новый — подходит.
    assert_refused(await login(kronto.browser(), user.email, PASSWORD))
    after = expect(await login(kronto.browser(), user.email, NEW_PASSWORD), 200)
    assert after["status"] in ("ok", "mfa_required"), after


async def test_password_reset_requires_app_code_when_app_enabled(
    kronto: Kronto,
) -> None:
    """ТЗ §3: при включённом приложении сброс пароля по почте требует и код
    приложения; без него и с неверным кодом пароль не меняется."""
    user = await register_and_confirm(
        kronto, "zakhar", first_name="Захар", last_name="Кузнецов"
    )
    secret = await kronto.enable_totp(user.email)
    before = await count_letters(kronto, user.email)
    expect(
        await kronto.browser().post(
            f"{API}/auth/forgot-password", json={"email": user.email}
        ),
        202,
    )
    token = await link_since(kronto, user.email, before)
    page = kronto.browser()
    url = f"{API}/auth/reset-password"

    # допущение: отказ из-за второго фактора не сжигает ссылку (иначе форму не пройти).
    assert_refused(
        await page.post(url, json={"token": token, "new_password": NEW_PASSWORD})
    )
    assert_refused(
        await page.post(
            url,
            json={
                "token": token,
                "new_password": NEW_PASSWORD,
                "second_factor": "000000",
            },
        )
    )
    still_old = expect(await login(kronto.browser(), user.email, PASSWORD), 200)
    assert still_old["status"] == "mfa_required", still_old

    body = expect(
        await page.post(
            url,
            json={
                "token": token,
                "new_password": NEW_PASSWORD,
                "second_factor": kronto.totp(secret),
            },
        ),
        200,
    )
    expect(await get_me(page, body["access_token"]), 200)
    assert_refused(await login(kronto.browser(), user.email, PASSWORD))


async def test_password_reset_accepts_backup_code(kronto: Kronto) -> None:
    """ТЗ §3: при включённом приложении сброс пароля требует его «или резервный
    код»."""
    user = await register_and_confirm(kronto, "inna")
    codes = await enable_totp_in_settings(kronto, user)
    assert codes, "при подключении приложения выдаются резервные коды"
    before = await count_letters(kronto, user.email)
    expect(
        await kronto.browser().post(
            f"{API}/auth/forgot-password", json={"email": user.email}
        ),
        202,
    )
    token = await link_since(kronto, user.email, before)

    page = kronto.browser()
    body = expect(
        await page.post(
            f"{API}/auth/reset-password",
            json={
                "token": token,
                "new_password": NEW_PASSWORD,
                "second_factor": codes[0],
            },
        ),
        200,
    )
    state = expect(
        await page.get(f"{API}/account/security", headers=bearer(body["access_token"])),
        200,
    )
    assert state["totp_enabled"] is True
    # допущение: резервный код одноразовый и при сбросе тоже сгорает.
    assert state["backup_codes_left"] == len(codes) - 1


async def test_change_password_refusals(kronto: Kronto) -> None:
    """ТЗ §4: пароль меняется в «Настройки → Безопасность»: нужен текущий пароль
    и вход; заведомо слабый новый пароль не принимается."""
    user = await register_and_confirm(kronto, "kostya")
    url = f"{API}/auth/change-password"

    assert_refused(
        await user.browser.post(
            url,
            headers=user.auth,
            json={"current_password": WRONG_PASSWORD, "new_password": NEW_PASSWORD},
        )
    )
    # допущение: см. WEAK_PASSWORD.
    assert_refused(
        await user.browser.post(
            url,
            headers=user.auth,
            json={"current_password": user.password, "new_password": WEAK_PASSWORD},
        )
    )
    anonymous = await kronto.browser().post(
        url, json={"current_password": user.password, "new_password": NEW_PASSWORD}
    )
    assert anonymous.status_code in (401, 403), show(anonymous)

    expect(await get_me(user.browser, user.token), 200)
    body = expect(await login(kronto.browser(), user.email, user.password), 200)
    assert body["status"] == "mfa_required", body


async def test_change_password_ends_other_sessions_and_notifies(kronto: Kronto) -> None:
    """ТЗ §3: письмо о безопасности при смене пароля; схема API: прочие сеансы
    закрываются, текущее устройство получает новый сеанс."""
    first = await register_and_confirm(kronto, "lidia")
    second = await login_by_email_code(kronto, first.email)
    before = await count_letters(kronto, first.email)

    body = expect(
        await second.browser.post(
            f"{API}/auth/change-password",
            headers=second.auth,
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        ),
        200,
    )
    expect(await get_me(second.browser, body["access_token"]), 200)
    # допущение: ТЗ говорит о выходе везде только для сброса; здесь — по описанию схемы.
    assert_refused(await get_me(first.browser, first.token))
    assert await count_letters(kronto, first.email) > before

    assert_refused(await login(kronto.browser(), first.email, PASSWORD))
    after = expect(await login(kronto.browser(), first.email, NEW_PASSWORD), 200)
    assert after["status"] in ("ok", "mfa_required"), after


# === §3, §4: приложение и резервные коды


async def test_enable_app_in_settings(kronto: Kronto) -> None:
    """ТЗ §3, §4: приложение-аутентификатор подключается в настройках, действует
    после ввода кода из приложения, выдаёт 10 резервных кодов; письмо о смене
    второго фактора."""
    user = await register_and_confirm(kronto, "marat")
    state = await security(user)
    assert state["totp_enabled"] is False
    assert state["passkeys"] == []

    setup = expect(
        await user.browser.post(f"{API}/account/totp/setup", headers=user.auth), 200
    )
    assert setup["otpauth_uri"].startswith("otpauth://totp/"), setup["otpauth_uri"]
    assert setup["secret"] in setup["otpauth_uri"]
    assert (await security(user))["totp_enabled"] is False

    url = f"{API}/account/totp/enable"
    wrong = await user.browser.post(
        url,
        headers=user.auth,
        json={"setup_token": setup["setup_token"], "code": "000000"},
    )
    assert_refused(wrong)
    assert (await security(user))["totp_enabled"] is False

    before = await count_letters(kronto, user.email)
    body = expect(
        await user.browser.post(
            url,
            headers=user.auth,
            json={
                "setup_token": setup["setup_token"],
                "code": kronto.totp(setup["secret"]),
            },
        ),
        200,
    )
    codes = body["backup_codes"]
    assert codes is not None and len(codes) == 10 and len(set(codes)) == 10, codes
    state = await security(user)
    assert state["totp_enabled"] is True
    assert state["backup_codes_left"] == 10
    assert await count_letters(kronto, user.email) > before


async def test_disable_app_requires_password_and_code(kronto: Kronto) -> None:
    """ТЗ §3, §4: отключить приложение можно в настройках, подтвердив паролем и
    кодом; после отключения вход снова по коду на почту; письмо о смене второго
    фактора."""
    user = await register_and_confirm(kronto, "nadia")
    await enable_totp_in_settings(kronto, user)
    assert user.totp_secret
    url = f"{API}/account/totp/disable"

    assert_refused(
        await user.browser.post(
            url,
            headers=user.auth,
            json={"password": WRONG_PASSWORD, "code": kronto.totp(user.totp_secret)},
        )
    )
    assert_refused(
        await user.browser.post(
            url, headers=user.auth, json={"password": user.password, "code": "000000"}
        )
    )
    assert (await security(user))["totp_enabled"] is True

    before = await count_letters(kronto, user.email)
    expect(
        await user.browser.post(
            url,
            headers=user.auth,
            json={"password": user.password, "code": kronto.totp(user.totp_secret)},
        ),
        204,
    )
    assert (await security(user))["totp_enabled"] is False
    assert await count_letters(kronto, user.email) > before

    mfa = await start_login(kronto.browser(), user.email)
    assert "email" in mfa["methods"], mfa
    assert "totp" not in mfa["methods"], mfa


async def test_regenerate_backup_codes_invalidates_old(kronto: Kronto) -> None:
    """ТЗ §3: 10 резервных кодов; новые коды — с паролем, старые перестают
    действовать (схема API)."""
    user = await register_and_confirm(kronto, "oksana")
    old = await enable_totp_in_settings(kronto, user)
    assert old
    url = f"{API}/account/backup-codes"

    assert_refused(
        await user.browser.post(
            url, headers=user.auth, json={"password": WRONG_PASSWORD}
        )
    )
    body = expect(
        await user.browser.post(
            url, headers=user.auth, json={"password": user.password}
        ),
        200,
    )
    new = body["backup_codes"]
    assert new is not None and len(new) == 10, new
    assert not set(new) & set(old)
    assert (await security(user))["backup_codes_left"] == 10

    browser = kronto.browser()
    mfa = await start_login(browser, user.email)
    assert_refused(await verify(browser, mfa["token"], "backup", code=old[0]))
    expect(await verify(browser, mfa["token"], "backup", code=new[0]), 200)


# === §3, §9: обязательный надёжный фактор


async def test_strong_factor_required_for_admins_and_by_company_policy(
    kronto: Kronto,
) -> None:
    """ТЗ §3: администраторам — обязательно приложение или ключ; админ может
    потребовать приложение или ключ от всех сотрудников (ТЗ §7 — правило в
    настройках компании); по умолчанию сотруднику хватает кода на почту.
    Сотрудник правило не меняет."""
    admin = await make_admin(kronto)
    assert (await security(admin))["strong_required"] is True
    profile = expect(await get_me(admin.browser, admin.token), 200)
    assert profile["mfa"]["strong_required"] is True
    assert profile["mfa"]["strong"] is True

    employee = await make_employee(kronto, admin, "sotrudnik")
    assert (await security(employee))["strong_required"] is False

    settings = expect(
        await admin.browser.get(f"{API}/company", headers=admin.auth), 200
    )
    assert settings["mfa_policy"] == "any"
    assert_refused(
        await employee.browser.patch(
            f"{API}/company", headers=employee.auth, json={"mfa_policy": "strong"}
        )
    )

    updated = expect(
        await admin.browser.patch(
            f"{API}/company", headers=admin.auth, json={"mfa_policy": "strong"}
        ),
        200,
    )
    assert updated["mfa_policy"] == "strong"
    assert (await security(employee))["strong_required"] is True


async def test_admin_without_app_or_key_cannot_use_admin_functions(
    kronto: Kronto,
) -> None:
    """ТЗ §3: администраторам компаний — обязательно приложение или ключ доступа.
    Без них админские функции недоступны; с приложением — доступны."""
    email = addr("admin-weak")
    await kronto.create_company(
        code="qa", name="QA", admin_email=email, admin_password=ADMIN_TEMP_PASSWORD
    )
    password = ADMIN_TEMP_PASSWORD
    browser = kronto.browser()

    # Два разумных решения: не пускать вовсе без приложения или ключа, либо
    # пустить по коду с почты, но закрыть админские функции до их подключения.
    token = await session_without_strong_factor(kronto, browser, email, password)
    if token is not None:
        profile = expect(await get_me(browser, token), 200)
        assert profile["mfa"]["strong_required"] is True
        assert profile["mfa"]["strong"] is False
        if profile["must_change_password"]:
            changed = await browser.post(
                f"{API}/auth/change-password",
                headers=bearer(token),
                json={"current_password": password, "new_password": PASSWORD},
            )
            if changed.status_code == 200:
                token = changed.json()["access_token"]
                password = PASSWORD
        for path in ("/users", "/invites"):
            denied = await browser.get(f"{API}{path}", headers=bearer(token))
            # допущение: код отказа не задан — любой 4xx.
            assert 400 <= denied.status_code < 500, show(denied)

    secret = await kronto.enable_totp(email)
    admin = await login_by_totp(kronto, email, secret, password=password)
    admin.tenant_id = await first_company_id(admin)
    await settle_admin(admin)
    expect(await admin.browser.get(f"{API}/users", headers=admin.auth), 200)


async def test_admin_login_does_not_accept_email_code(kronto: Kronto) -> None:
    """ТЗ §3: администратору обязательно приложение или ключ — код на почту при
    его входе не предлагается и не принимается."""
    admin = await make_admin(kronto)
    browser = kronto.browser()
    mfa = await start_login(browser, admin.email, admin.password)
    assert "email" not in mfa["methods"], mfa
    assert "totp" in mfa["methods"], mfa
    assert_refused(await verify(browser, mfa["token"], "email", code="123456"))


async def test_admin_cannot_drop_last_strong_factor(kronto: Kronto) -> None:
    """ТЗ §3: администратору приложение или ключ обязательны — отключив
    единственный надёжный фактор, админом без него остаться нельзя."""
    admin = await make_admin(kronto)
    assert admin.totp_secret
    r = await admin.browser.post(
        f"{API}/account/totp/disable",
        headers=admin.auth,
        json={"password": admin.password, "code": kronto.totp(admin.totp_secret)},
    )
    if r.status_code < 400:
        # допущение: второе разумное решение — отключить дали, но админские
        # функции сразу закрыты до нового приложения или ключа.
        denied = await admin.browser.get(f"{API}/users", headers=admin.auth)
        assert 400 <= denied.status_code < 500, show(denied)
    else:
        assert_refused(r)
        assert (await security(admin))["totp_enabled"] is True


async def test_staff_panel_needs_app_or_key(kronto: Kronto) -> None:
    """ТЗ §3, §9: команде kronto — обязательно приложение или ключ; в нашу панель
    вход только с ними."""
    user = await register_and_confirm(
        kronto, "staff", first_name="Артём", last_name="Командный"
    )
    await kronto.make_staff(user.email)

    weak = await user.browser.get(f"{API}/staff/overview", headers=user.auth)
    assert 400 <= weak.status_code < 500, show(weak)

    browser = kronto.browser()
    token = await session_without_strong_factor(
        kronto, browser, user.email, user.password
    )
    if token is not None:
        denied = await browser.get(f"{API}/staff/overview", headers=bearer(token))
        assert 400 <= denied.status_code < 500, show(denied)

    secret = await kronto.enable_totp(user.email)
    staff = await login_by_totp(kronto, user.email, secret)
    profile = expect(await get_me(staff.browser, staff.token), 200)
    assert profile["staff"] is True
    expect(await staff.browser.get(f"{API}/staff/overview", headers=staff.auth), 200)


# === §3: смена почты


async def test_email_change_without_app(kronto: Kronto) -> None:
    """ТЗ §3: смена почты — изнутри учётки, с паролем и вторым фактором (без
    приложения — код на прежний адрес); подтверждение на новый адрес; до
    подтверждения почта прежняя."""
    user = await register_and_confirm(
        kronto, "ivan-old", first_name="Иван", last_name="Петров"
    )
    new_email = addr("ivan-new")

    token = await change_email_until_link(kronto, user, new_email)
    assert expect(await get_me(user.browser, user.token), 200)["email"] == user.email

    expect(
        await kronto.browser().post(
            f"{API}/account/email/confirm", json={"token": token}
        ),
        204,
    )

    moved = await login_by_email_code(kronto, new_email)
    assert expect(await get_me(moved.browser, moved.token), 200)["email"] == new_email
    assert_refused(await login(kronto.browser(), user.email))


async def test_email_change_refusals(kronto: Kronto) -> None:
    """ТЗ §3: смена почты — только изнутри учётки, с верным паролем и верным
    вторым фактором; выдуманная ссылка подтверждения не срабатывает."""
    user = await register_and_confirm(
        kronto, "pyotr", first_name="Пётр", last_name="Ракитин"
    )
    new_email = addr("pyotr-new")

    anonymous = await kronto.browser().post(
        f"{API}/account/email", json={"new_email": new_email, "password": user.password}
    )
    assert anonymous.status_code in (401, 403), show(anonymous)
    assert_refused(await request_email_change(user, new_email, password=WRONG_PASSWORD))

    before_old = await count_letters(kronto, user.email)
    first = expect(await request_email_change(user, new_email), 202)
    assert first["status"] == "code_sent", first
    code = await code_since(kronto, user.email, before_old)
    assert_refused(await request_email_change(user, new_email, code=other_code(code)))
    assert await count_letters(kronto, new_email) == 0

    assert_refused(
        await kronto.browser().post(
            f"{API}/account/email/confirm", json={"token": "Q1" + "x" * 30}
        )
    )
    assert expect(await get_me(user.browser, user.token), 200)["email"] == user.email


async def test_email_change_revert_from_old_address(kronto: Kronto) -> None:
    """ТЗ §3: письмо на старый адрес со ссылкой «это не я» — отменяет смену и
    выходит со всех устройств."""
    user = await register_and_confirm(
        kronto, "sofia-old", first_name="Софья", last_name="Ветрова"
    )
    new_email = addr("sofia-new")
    before_old = await count_letters(kronto, user.email)

    token = await change_email_until_link(kronto, user, new_email)
    expect(
        await kronto.browser().post(
            f"{API}/account/email/confirm", json={"token": token}
        ),
        204,
    )

    old_letters = await letters_since(kronto, user.email, before_old)
    revert_tokens = [
        tokens[0] for tokens in map(link_tokens, reversed(old_letters)) if tokens
    ]
    assert revert_tokens, "на прежний адрес должно прийти письмо со ссылкой «это не я»"
    expect(
        await kronto.browser().post(
            f"{API}/account/email/revert", json={"token": revert_tokens[0]}
        ),
        204,
    )

    assert_refused(await get_me(user.browser, user.token))
    assert_refused(await refresh(user.browser))
    back = expect(await login(kronto.browser(), user.email), 200)
    assert back["status"] == "mfa_required", back
    assert_refused(await login(kronto.browser(), new_email))


async def test_email_change_with_app_code(kronto: Kronto) -> None:
    """ТЗ §3: смена почты с паролем и вторым фактором — у кого приложение, тот
    вводит код из приложения; подтверждение — по ссылке на новый адрес."""
    user = await register_and_confirm(
        kronto, "timur-old", first_name="Тимур", last_name="Алиев"
    )
    secret = await kronto.enable_totp(user.email)
    new_email = addr("timur-new")

    body = expect(
        await request_email_change(user, new_email, code=kronto.totp(secret)), 202
    )
    assert body["status"] == "link_sent", body
    token = await link_since(kronto, new_email, 0)
    expect(
        await kronto.browser().post(
            f"{API}/account/email/confirm", json={"token": token}
        ),
        204,
    )

    mfa = await start_login(kronto.browser(), new_email)
    assert "totp" in mfa["methods"], mfa


async def test_email_change_to_taken_address_does_not_happen(kronto: Kronto) -> None:
    """ТЗ §2: учётка у человека одна, почта — её ключ: сменой почты нельзя
    забрать адрес чужой учётки."""
    user = await register_and_confirm(
        kronto, "ruslan", first_name="Руслан", last_name="Ткачёв"
    )
    other = await register_and_confirm(
        kronto, "svetlana", first_name="Светлана", last_name="Юдина"
    )
    before_old = await count_letters(kronto, user.email)
    before_other = await count_letters(kronto, other.email)

    r = await request_email_change(user, other.email)
    if r.status_code == 202 and r.json()["status"] == "code_sent":
        code = await code_since(kronto, user.email, before_old)
        r = await request_email_change(user, other.email, code=code)
    if r.status_code == 202 and r.json()["status"] == "link_sent":
        # допущение: письмо могли отправить, чтобы не выдать занятость адреса,
        # но подтвердить чужой адрес нельзя.
        for letter in await letters_since(kronto, other.email, before_other):
            for token in link_tokens(letter)[:1]:
                confirm = await kronto.browser().post(
                    f"{API}/account/email/confirm", json={"token": token}
                )
                assert confirm.status_code >= 400, show(confirm)
    else:
        assert_refused(r)

    assert expect(await get_me(user.browser, user.token), 200)["email"] == user.email
    assert expect(await get_me(other.browser, other.token), 200)["email"] == other.email


# === §2: учётка, компании, удаление


async def test_login_without_company_code_lands_in_company(kronto: Kronto) -> None:
    """ТЗ §3: вход — почта + пароль + второй фактор, без кода компании (код с
    формы входа убран, §2); человек сразу в своей компании, роль — у членства."""
    email = addr("admin-qa")
    company = await kronto.create_company(
        code="qa", name="QA", admin_email=email, admin_password=ADMIN_TEMP_PASSWORD
    )
    assert company["account_created"] is True
    secret = await kronto.enable_totp(email)

    with_code = await kronto.browser().post(
        f"{API}/auth/login",
        json={"email": email, "password": ADMIN_TEMP_PASSWORD, "company": "qa"},
    )
    assert with_code.status_code == 422, show(with_code)

    admin = await login_by_totp(kronto, email, secret, password=ADMIN_TEMP_PASSWORD)
    profile = expect(await get_me(admin.browser, admin.token), 200)
    assert profile["company"] is not None, profile
    assert str(profile["company"]["tenant_id"]) == str(company["id"])
    assert profile["company"]["role"] == "admin"
    assert [str(item["tenant_id"]) for item in profile["companies"]] == [
        str(company["id"])
    ]


async def test_one_account_in_two_companies_and_switching(kronto: Kronto) -> None:
    """ТЗ §2: учётка не зависит от компании; человек состоит в нескольких
    компаниях со своим паролем и переключается между ними; в чужую компанию
    переключиться нельзя."""
    user = await register_and_confirm(
        kronto, "maria", first_name="Мария", last_name="Двойная"
    )
    secret = await kronto.enable_totp(user.email)
    alpha = await kronto.create_company(
        code="alpha", name="Альфа", admin_email=user.email
    )
    beta = await kronto.create_company(code="beta", name="Бета", admin_email=user.email)
    assert alpha["account_created"] is False
    assert beta["account_created"] is False
    gamma = await kronto.create_company(
        code="gamma",
        name="Гамма",
        admin_email=addr("admin-gamma"),
        admin_password=ADMIN_TEMP_PASSWORD,
    )

    # Вход своим паролем: временный пароль существующей учётке не назначается.
    person = await login_by_totp(kronto, user.email, secret)
    profile = expect(await get_me(person.browser, person.token), 200)
    assert {str(item["tenant_id"]) for item in profile["companies"]} == {
        str(alpha["id"]),
        str(beta["id"]),
    }
    assert all(item["role"] == "admin" for item in profile["companies"])

    current = str(profile["company"]["tenant_id"]) if profile["company"] else None
    target = str(beta["id"]) if current != str(beta["id"]) else str(alpha["id"])
    body = expect(
        await person.browser.post(
            f"{API}/auth/switch-company",
            headers=person.auth,
            json={"tenant_id": target},
        ),
        200,
    )
    token = body["access_token"]
    assert (
        str(expect(await get_me(person.browser, token), 200)["company"]["tenant_id"])
        == target
    )

    assert_refused(
        await person.browser.post(
            f"{API}/auth/switch-company",
            headers=bearer(token),
            json={"tenant_id": str(gamma["id"])},
        )
    )
    assert (
        str(expect(await get_me(person.browser, token), 200)["company"]["tenant_id"])
        == target
    )


async def test_delete_account(kronto: Kronto) -> None:
    """ТЗ §2: удаление учётки — сам человек, в «Управление учётной записью», с
    подтверждением паролем; после удаления не войти."""
    user = await register_and_confirm(
        kronto, "fyodor-del", first_name="Фёдор", last_name="Удалов"
    )
    url = f"{API}/account/delete"

    anonymous = await kronto.browser().post(url, json={"password": user.password})
    assert anonymous.status_code in (401, 403), show(anonymous)
    assert_refused(
        await user.browser.post(
            url, headers=user.auth, json={"password": WRONG_PASSWORD}
        )
    )
    expect(await get_me(user.browser, user.token), 200)

    expect(
        await user.browser.post(
            url, headers=user.auth, json={"password": user.password}
        ),
        204,
    )
    assert_refused(await get_me(user.browser, user.token))
    assert_refused(await login(kronto.browser(), user.email))


async def test_last_admin_must_appoint_another_before_deleting(kronto: Kronto) -> None:
    """ТЗ §2: если человек — последний администратор компании, сначала назначить
    другого; после назначения удалить учётку можно."""
    admin = await make_admin(kronto)
    url = f"{API}/account/delete"
    assert_refused(
        await admin.browser.post(
            url, headers=admin.auth, json={"password": admin.password}
        )
    )
    expect(await get_me(admin.browser, admin.token), 200)

    employee = await make_employee(kronto, admin, "zamestitel")
    await kronto.enable_totp(employee.email)
    people = expect(await admin.browser.get(f"{API}/users", headers=admin.auth), 200)
    member = next(item for item in people if item["email"] == employee.email)
    promoted = expect(
        await admin.browser.patch(
            f"{API}/users/{member['id']}", headers=admin.auth, json={"role": "admin"}
        ),
        200,
    )
    assert promoted["role"] == "admin"

    expect(
        await admin.browser.post(
            url, headers=admin.auth, json={"password": admin.password}
        ),
        204,
    )
    assert_refused(await login(kronto.browser(), admin.email, admin.password))


async def test_company_request_lifecycle(kronto: Kronto) -> None:
    """ТЗ §2: человек без компании подаёт заявку «Подключить компанию» (одобряем
    мы); свою заявку он видит и может отменить, чужую — нет."""
    user = await register_and_confirm(
        kronto, "gennady", first_name="Геннадий", last_name="Заявкин"
    )
    other = await register_and_confirm(
        kronto, "polina", first_name="Полина", last_name="Сторонняя"
    )
    url = f"{API}/account/company-requests"

    anonymous = await kronto.browser().post(url, json={"company_name": "ООО Ромашка"})
    assert anonymous.status_code in (401, 403), show(anonymous)
    too_short = await user.browser.post(
        url, headers=user.auth, json={"company_name": "А"}
    )
    assert too_short.status_code == 422, show(too_short)

    created = expect(
        await user.browser.post(
            url,
            headers=user.auth,
            json={"company_name": "ООО Ромашка", "seats": 15, "comment": "Хотим пилот"},
        ),
        201,
    )
    assert created["status"] == "new"
    assert created["company_name"] == "ООО Ромашка"
    mine = expect(await user.browser.get(url, headers=user.auth), 200)
    assert [str(item["id"]) for item in mine] == [str(created["id"])]
    assert expect(await other.browser.get(url, headers=other.auth), 200) == []

    cancel = f"{url}/{created['id']}/cancel"
    assert_refused(await other.browser.post(cancel, headers=other.auth))
    assert (
        expect(await user.browser.get(url, headers=user.auth), 200)[0]["status"]
        == "new"
    )
    cancelled = expect(await user.browser.post(cancel, headers=user.auth), 200)
    assert cancelled["status"] == "cancelled"


async def test_profile_first_and_last_name_cannot_be_emptied(kronto: Kronto) -> None:
    """ТЗ §4: в профиле обязательно имя и фамилия, остальное (отчество и т. п.) —
    по желанию."""
    user = await register_and_confirm(
        kronto, "anfisa", first_name="Анфиса", last_name="Полякова"
    )
    url = f"{API}/account"

    empty = await user.browser.patch(url, headers=user.auth, json={"first_name": ""})
    assert empty.status_code == 422, show(empty)
    cleared = await user.browser.patch(
        url, headers=user.auth, json={"first_name": None, "last_name": None}
    )
    # допущение: схема разрешает null («очистить»), но ТЗ делает имя обязательным —
    # годится отказ или игнорирование, но не пустое имя.
    assert cleared.status_code < 500, show(cleared)
    profile = expect(await get_me(user.browser, user.token), 200)
    assert profile["first_name"] == "Анфиса"
    assert profile["last_name"] == "Полякова"

    expect(
        await user.browser.patch(
            url,
            headers=user.auth,
            json={"first_name": "Мария", "patronymic": "Ивановна"},
        ),
        204,
    )
    profile = expect(await get_me(user.browser, user.token), 200)
    assert profile["first_name"] == "Мария"
    assert profile["patronymic"] == "Ивановна"
    assert profile["last_name"] == "Полякова"

    expect(
        await user.browser.patch(url, headers=user.auth, json={"patronymic": None}), 204
    )
    assert expect(await get_me(user.browser, user.token), 200)["patronymic"] is None
