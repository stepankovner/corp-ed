"""Отделы и «Подтверждение отдела» (ТЗ §7) и их связь с §4, §5, §6, §8.

Тесты «чёрного ящика»: только HTTP API и фикстура kronto (HARNESS.md).
Закрытая папка открыта человеку, только если его отдел подтверждён
администратором; администраторам открыто всё.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass

import httpx

from tests.blackbox.conftest import API, Kronto

ADMIN_TEMP_PASSWORD = "Vremennyj-Klyuch-Rukovoditelya-2026!"
ADMIN_NEW_PASSWORD = "Novyj-Klyuch-Rukovoditelya-2026-kronto!"
PASSWORD = "Lichnyj-Parol-Sotrudnika-2026!"


# --- Данные -----------------------------------------------------------------


@dataclass
class Company:
    id: str
    code: str
    admin: httpx.AsyncClient
    admin_email: str
    admin_member_id: str
    invite: str

    async def admin_user_id(self) -> str:
        """id администратора в «Людях» (/users/{user_id})."""
        return await _user_id(self.admin, self.admin_email)


@dataclass
class Person:
    browser: httpx.AsyncClient
    email: str
    first_name: str
    member_id: str  # для /people/{member_id}
    company: Company

    async def user_id(self) -> str:
        """id в «Людях» (/users/{user_id}) — из списка администратора."""
        return await _user_id(self.company.admin, self.email)


@dataclass
class Doc:
    """Документ с отличительным кодом и вопросом теми же словами."""

    material_id: str
    folder_id: str | None
    question: str
    code: str


@dataclass
class World:
    company: Company
    dept_a: str  # «Бухгалтерия»: ей открыта закрытая папка с secret_a
    dept_b: str  # «Логистика»: ей открыта закрытая папка с secret_b
    dept_c: str  # «Маркетинг»: закрытых папок нет
    folder_a: str
    folder_b: str
    open_folder: str
    secret_a: Doc
    secret_b: Doc
    public: Doc  # документ без папки
    open_doc: Doc  # документ обычной (не закрытой) папки


# Тексты документов: у каждого свой код в начале и свои слова; вопрос —
# теми же словоформами, общих слов с чужими документами нет.
SECRET_A_TEXT = "Шифр сейфа бухгалтерии 7931. Шифр меняет главный бухгалтер."
SECRET_A_QUESTION = "Какой шифр сейфа бухгалтерии?"
SECRET_B_TEXT = "Пароль склада логистики 2648. Его сообщает начальник смены."
SECRET_B_QUESTION = "Какой пароль склада логистики?"
PUBLIC_TEXT = "Пропуск на парковку 5520 выдаёт охрана в холле."
PUBLIC_QUESTION = "Кто выдаёт пропуск на парковку?"
OPEN_TEXT = "Столовая работает с полудня, скидка 3184 по карте."
OPEN_QUESTION = "Когда работает столовая?"
LATER_TEXT = "Бюджет маркетинга 4417 утверждён советом директоров."
LATER_QUESTION = "Каков бюджет маркетинга?"


# --- Вход, компания, люди ---------------------------------------------------


def _bearer(browser: httpx.AsyncClient, token: str) -> None:
    browser.headers["Authorization"] = f"Bearer {token}"


async def _login(
    kronto: Kronto,
    email: str,
    password: str,
    totp_secret: str,
    tenant_id: str,
) -> httpx.AsyncClient:
    """Вход с паролем и приложением-аутентификатором (ТЗ §3)."""
    browser = kronto.browser()
    response = await browser.post(
        f"{API}/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    if body["status"] == "mfa_required":
        challenge = body["mfa"]
        if "totp" in challenge["methods"]:
            payload = {
                "token": challenge["token"],
                "method": "totp",
                "code": kronto.totp(totp_secret),
            }
        else:
            letter = await kronto.last_letter(email)
            payload = {
                "token": challenge["token"],
                "method": "email",
                "code": _six_digits(letter.text),
            }
        response = await browser.post(f"{API}/auth/mfa/verify", json=payload)
        assert response.status_code == 200, response.text
        body = response.json()
    _bearer(browser, body["access_token"])

    me = (await browser.get(f"{API}/auth/me")).json()
    if me["must_change_password"]:
        response = await browser.post(
            f"{API}/auth/change-password",
            json={"current_password": password, "new_password": ADMIN_NEW_PASSWORD},
        )
        assert response.status_code == 200, response.text
        _bearer(browser, response.json()["access_token"])
        me = (await browser.get(f"{API}/auth/me")).json()
    if me["company"] is None or me["company"]["tenant_id"] != tenant_id:
        response = await browser.post(
            f"{API}/auth/switch-company", json={"tenant_id": tenant_id}
        )
        assert response.status_code == 200, response.text
        _bearer(browser, response.json()["access_token"])
    return browser


def _six_digits(text: str) -> str:
    match = re.search(r"(?<!\d)\d{6}(?!\d)", text)
    assert match, f"в письме нет кода из 6 цифр: {text!r}"
    return match.group(0)


async def _user_id(admin: httpx.AsyncClient, email: str) -> str:
    response = await admin.get(f"{API}/users")
    assert response.status_code == 200, response.text
    for row in response.json():
        if row["email"] == email:
            return row["id"]
    raise AssertionError(f"{email} нет в списке людей компании")


async def new_company(kronto: Kronto, code: str) -> Company:
    """Компания с администратором, вошедшим с приложением (ТЗ §3)."""
    email = f"admin@{code}-corp.ru"
    created = await kronto.create_company(
        code=code,
        name=f"ООО {code.capitalize()}",
        admin_email=email,
        admin_password=ADMIN_TEMP_PASSWORD,
    )
    secret = await kronto.enable_totp(email)
    admin = await _login(kronto, email, ADMIN_TEMP_PASSWORD, secret, created["id"])
    me = (await admin.get(f"{API}/auth/me")).json()
    response = await admin.post(f"{API}/invites", json={"max_uses": 50})
    assert response.status_code == 201, response.text
    return Company(
        id=created["id"],
        code=code,
        admin=admin,
        admin_email=email,
        admin_member_id=me["company"]["member_id"],
        invite=response.json()["token"],
    )


async def join(kronto: Kronto, company: Company, alias: str, first_name: str) -> Person:
    """Регистрация, подтверждение почты и вступление по приглашению (ТЗ §2)."""
    email = f"{alias}@{company.code}-corp.ru"
    browser = kronto.browser()
    response = await browser.post(
        f"{API}/auth/register",
        json={
            "first_name": first_name,
            "last_name": "Тестова",
            "email": email,
            "password": PASSWORD,
            "consent": True,
        },
    )
    assert response.status_code == 202, response.text
    letter = await kronto.last_letter(email)
    response = await browser.post(
        f"{API}/auth/verify-email",
        json={"email": email, "code": _six_digits(letter.text)},
    )
    assert response.status_code == 200, response.text
    _bearer(browser, response.json()["access_token"])
    response = await browser.post(
        f"{API}/invites/accept", json={"secret": company.invite}
    )
    assert response.status_code == 200, response.text
    joined = response.json()
    assert joined["outcome"] == "joined", joined
    _bearer(browser, joined["session"]["access_token"])
    me = (await browser.get(f"{API}/auth/me")).json()
    return Person(
        browser=browser,
        email=email,
        first_name=first_name,
        member_id=me["company"]["member_id"],
        company=company,
    )


async def promote_to_admin(
    kronto: Kronto, company: Company, person: Person
) -> httpx.AsyncClient:
    """Второй администратор: приложение, роль, новый вход (ТЗ §2, §3)."""
    secret = await kronto.enable_totp(person.email)
    response = await company.admin.patch(
        f"{API}/users/{await person.user_id()}", json={"role": "admin"}
    )
    assert response.status_code == 200, response.text
    return await _login(kronto, person.email, PASSWORD, secret, company.id)


# --- Отделы, папки, документы ----------------------------------------------


async def create_department(admin: httpx.AsyncClient, name: str) -> str:
    response = await admin.post(f"{API}/departments", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def create_folder(
    admin: httpx.AsyncClient,
    name: str,
    *,
    restricted: bool,
    departments: list[str] | None = None,
) -> str:
    response = await admin.post(
        f"{API}/folders",
        json={
            "name": name,
            "restricted": restricted,
            "department_ids": departments or [],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def add_document(
    admin: httpx.AsyncClient,
    title: str,
    text: str,
    folder_id: str | None,
) -> str:
    """Документ текстом (POST /materials); индексируется в фоне."""
    payload: dict[str, str] = {"title": title, "content": text}
    if folder_id is not None:
        payload["folder_id"] = folder_id
    response = await admin.post(f"{API}/materials", json=payload)
    assert response.status_code == 201, response.text
    material = response.json()
    assert material.get("folder_id") == folder_id, material
    return material["id"]


async def index_all(kronto: Kronto, admin: httpx.AsyncClient, *docs: Doc) -> None:
    """Фоновая индексация: без неё проверка «не видно» ничего не доказывает."""
    await kronto.run_background()
    for doc in docs:
        response = await admin.get(f"{API}/materials/{doc.material_id}")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "ready", response.json()


async def build_world(kronto: Kronto, code: str = "romashka") -> World:
    company = await new_company(kronto, code)
    admin = company.admin
    dept_a = await create_department(admin, "Бухгалтерия")
    dept_b = await create_department(admin, "Логистика")
    dept_c = await create_department(admin, "Маркетинг")
    folder_a = await create_folder(
        admin, "Бухгалтерия — закрыто", restricted=True, departments=[dept_a]
    )
    folder_b = await create_folder(
        admin, "Логистика — закрыто", restricted=True, departments=[dept_b]
    )
    open_folder = await create_folder(admin, "Общие регламенты", restricted=False)
    secret_a = Doc(
        await add_document(admin, "Сейф", SECRET_A_TEXT, folder_a),
        folder_a,
        SECRET_A_QUESTION,
        "7931",
    )
    secret_b = Doc(
        await add_document(admin, "Склад", SECRET_B_TEXT, folder_b),
        folder_b,
        SECRET_B_QUESTION,
        "2648",
    )
    public = Doc(
        await add_document(admin, "Парковка", PUBLIC_TEXT, None),
        None,
        PUBLIC_QUESTION,
        "5520",
    )
    open_doc = Doc(
        await add_document(admin, "Столовая", OPEN_TEXT, open_folder),
        open_folder,
        OPEN_QUESTION,
        "3184",
    )
    await index_all(kronto, admin, secret_a, secret_b, public, open_doc)
    return World(
        company=company,
        dept_a=dept_a,
        dept_b=dept_b,
        dept_c=dept_c,
        folder_a=folder_a,
        folder_b=folder_b,
        open_folder=open_folder,
        secret_a=secret_a,
        secret_b=secret_b,
        public=public,
        open_doc=open_doc,
    )


# --- Профиль, люди, отделы --------------------------------------------------


async def set_department(
    browser: httpx.AsyncClient, member_id: str, department_id: str | None
) -> httpx.Response:
    return await browser.patch(
        f"{API}/people/{member_id}", json={"department_id": department_id}
    )


async def pick_department(person: Person, department_id: str | None) -> dict:
    """Сотрудник сам выбирает (или убирает) свой отдел в профиле (ТЗ §4)."""
    response = await set_department(person.browser, person.member_id, department_id)
    assert response.status_code == 200, response.text
    return response.json()


async def assign_department(
    company: Company, member_id: str, department_id: str | None
) -> dict:
    """Администратор назначает отдел (ТЗ §7)."""
    response = await set_department(company.admin, member_id, department_id)
    assert response.status_code == 200, response.text
    return response.json()


async def open_shared(browser: httpx.AsyncClient, token: str) -> httpx.Response:
    """Общая ссылка: токен — в теле запроса, не в адресе (схема)."""
    return await browser.post(f"{API}/conversations/shared/open", json={"token": token})


async def my_company(browser: httpx.AsyncClient) -> dict:
    response = await browser.get(f"{API}/auth/me")
    assert response.status_code == 200, response.text
    return response.json()["company"]


async def user_row(company: Company, user_id: str) -> dict:
    response = await company.admin.get(f"{API}/users")
    assert response.status_code == 200, response.text
    for row in response.json():
        if row["id"] == user_id:
            return row
    raise AssertionError(f"{user_id} нет в /users")


async def directory_entry(browser: httpx.AsyncClient, member_id: str) -> dict:
    response = await browser.get(f"{API}/people")
    assert response.status_code == 200, response.text
    for row in response.json():
        if row["member_id"] == member_id:
            return row
    raise AssertionError(f"{member_id} нет в справочнике")


async def department_counts(
    browser: httpx.AsyncClient, department_id: str
) -> tuple[int, int | None]:
    response = await browser.get(f"{API}/departments")
    assert response.status_code == 200, response.text
    for row in response.json():
        if row["id"] == department_id:
            return row["members"], row.get("unconfirmed")
    raise AssertionError(f"отдела {department_id} нет в списке")


async def _target(who: Person | str) -> str:
    return who if isinstance(who, str) else await who.user_id()


async def _seen_department(browser: httpx.AsyncClient, user_id: str) -> str:
    """Отдел человека, каким его видит вызывающий в «Людях» (ТЗ §7:
    решение — про тот отдел, что видел администратор). Не видно (не админ,
    чужой, без отдела) — случайный id: сервер всё равно откажет."""
    response = await browser.get(f"{API}/users")
    if response.status_code == 200:
        for row in response.json():
            if row["id"] == user_id and row.get("department_id"):
                return str(row["department_id"])
    return str(uuid.uuid4())


async def confirm(
    browser: httpx.AsyncClient, who: Person | str, department_id: str | None = None
) -> httpx.Response:
    """Подтвердить отдел человека (кто вызывает — тот и решает)."""
    user_id = await _target(who)
    seen = department_id or await _seen_department(browser, user_id)
    return await browser.post(
        f"{API}/users/{user_id}/department/confirm", json={"department_id": seen}
    )


async def reject(
    browser: httpx.AsyncClient, who: Person | str, department_id: str | None = None
) -> httpx.Response:
    """Отклонить отдел человека (кто вызывает — тот и решает)."""
    user_id = await _target(who)
    seen = department_id or await _seen_department(browser, user_id)
    return await browser.post(
        f"{API}/users/{user_id}/department/reject", json={"department_id": seen}
    )


def _dept_id(body: dict) -> str | None:
    department = body.get("department")
    return department["id"] if department else None


async def assert_state(
    company: Company,
    person: Person,
    department_id: str | None,
    confirmed: bool,
) -> None:
    """Отдел и подтверждение — одинаково в профиле, справочнике и «Людях»."""
    mine = await my_company(person.browser)
    assert _dept_id(mine) == department_id, mine
    assert mine.get("department_confirmed") is confirmed, mine
    row = await user_row(company, await person.user_id())
    assert row.get("department_id") == department_id, row
    assert row.get("department_confirmed") is confirmed, row
    entry = await directory_entry(company.admin, person.member_id)
    assert _dept_id(entry) == department_id, entry
    assert entry.get("department_confirmed") is confirmed, entry


# --- Ассистент и видимость документов ---------------------------------------


async def ask(browser: httpx.AsyncClient, question: str) -> dict:
    response = await browser.post(f"{API}/faq/ask", json={"question": question})
    assert response.status_code == 200, response.text
    return response.json()


def _sse_events(text: str) -> list[dict]:
    return [
        json.loads(line[len("data:") :].strip())
        for line in text.splitlines()
        if line.startswith("data:")
    ]


async def chat(browser: httpx.AsyncClient, question: str) -> tuple[str, dict]:
    """Новый диалог (ТЗ §6): id диалога и итоговый ответ из потока."""
    response = await browser.post(f"{API}/conversations", json={"question": question})
    assert response.status_code == 200, response.text
    events = _sse_events(response.text)
    assert not [e for e in events if e["type"] == "error"], events
    start = next(e for e in events if e["type"] == "start")
    done = next(e for e in events if e["type"] == "done")
    return start["conversation"]["id"], done["answer"]


async def folder_documents(browser: httpx.AsyncClient, folder_id: str | None) -> int:
    """Сколько документов папки сотрудник видит в «где ищет ассистент» (§5)."""
    response = await browser.get(f"{API}/sources/mine")
    assert response.status_code == 200, response.text
    for group in response.json()["files"]:
        if group["folder_id"] == folder_id:
            return group["documents"]
    return 0


def _assert_answer_hides(answer: dict, doc: Doc) -> None:
    assert doc.code not in answer["content"], answer
    for source in answer["sources"]:
        assert source.get("material_id") != doc.material_id, answer
        assert doc.code not in (source.get("content") or ""), answer


async def assert_hidden(
    browser: httpx.AsyncClient, doc: Doc, *, in_chat: bool = False
) -> None:
    """Документ закрытой папки не виден: ни в ответах, ни в источниках,
    ни в списках документов (ТЗ §7)."""
    _assert_answer_hides(await ask(browser, doc.question), doc)
    if in_chat:
        _, answer = await chat(browser, doc.question)
        _assert_answer_hides(answer, doc)
    assert await folder_documents(browser, doc.folder_id) == 0
    response = await browser.get(f"{API}/materials")
    if response.status_code == 200:
        assert doc.material_id not in {m["id"] for m in response.json()}
    else:
        # допущение: список документов сотруднику может быть закрыт целиком
        assert response.status_code in (403, 404), response.text
    response = await browser.get(f"{API}/materials/{doc.material_id}")
    # допущение: недоступный документ — 403 или 404
    assert response.status_code in (403, 404), response.text


async def assert_visible(
    browser: httpx.AsyncClient, doc: Doc, *, in_chat: bool = False
) -> None:
    """Документ виден сотруднику: ассистент находит его, он в источниках."""
    answer = await ask(browser, doc.question)
    sources = {s.get("material_id") for s in answer["sources"]}
    assert doc.material_id in sources, answer
    assert doc.code in answer["content"], answer
    if in_chat:
        _, answer = await chat(browser, doc.question)
        assert doc.material_id in {s.get("material_id") for s in answer["sources"]}
        assert doc.code in answer["content"], answer
    assert await folder_documents(browser, doc.folder_id) >= 1


async def assert_admin_sees(admin: httpx.AsyncClient, doc: Doc) -> None:
    answer = await ask(admin, doc.question)
    assert doc.material_id in {s.get("material_id") for s in answer["sources"]}
    assert doc.code in answer["content"], answer
    response = await admin.get(f"{API}/materials")
    assert response.status_code == 200, response.text
    assert doc.material_id in {m["id"] for m in response.json()}


# --- Уведомления, письма, журнал --------------------------------------------


async def notifications(
    kronto: Kronto, browser: httpx.AsyncClient, kind: str
) -> list[dict]:
    """Колокольчик (§8) после работы воркера: доставка может идти фоном."""
    await kronto.run_background()
    response = await browser.get(f"{API}/notifications")
    assert response.status_code == 200, response.text
    return [n for n in response.json()["items"] if n["kind"] == kind]


async def set_join_request_emails(admin: httpx.AsyncClient, enabled: bool) -> None:
    response = await admin.put(
        f"{API}/notifications/settings", json={"email_join_requests": enabled}
    )
    assert response.status_code == 200, response.text
    assert response.json()["email_join_requests"] is enabled


async def audit_events(admin: httpx.AsyncClient) -> list[dict]:
    response = await admin.get(f"{API}/audit", params={"limit": 200})
    assert response.status_code == 200, response.text
    return response.json()


# ============================================================================
# Назначение администратором
# ============================================================================


async def test_admin_assigned_department_is_confirmed_at_once(
    kronto: Kronto,
) -> None:
    """ТЗ §7: отдел, который назначил администратор, подтверждён сразу —
    сотруднику открывается закрытая папка отдела (ответы, источники,
    «где ищет ассистент»)."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    await assert_hidden(anna.browser, world.secret_a)

    body = await assign_department(company, anna.member_id, world.dept_a)

    assert _dept_id(body) == world.dept_a
    assert body.get("department_confirmed") is True, body
    await assert_state(company, anna, world.dept_a, True)
    await assert_visible(anna.browser, world.secret_a, in_chat=True)
    # Закрытая папка другого отдела по-прежнему закрыта.
    await assert_hidden(anna.browser, world.secret_b)


async def test_admin_assignment_sends_no_confirmation_request(
    kronto: Kronto,
) -> None:
    """ТЗ §7, §8: «ждёт подтверждения» — только про отдел, выбранный самим
    сотрудником; назначенный администратором уже подтверждён."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")

    await assign_department(company, anna.member_id, world.dept_a)

    # допущение: назначение админом не рождает запроса на подтверждение
    assert await notifications(kronto, company.admin, "department_request") == []
    assert (await department_counts(company.admin, world.dept_a)) == (1, 0)


# ============================================================================
# Выбор отдела самим сотрудником
# ============================================================================


async def test_self_selected_department_shown_but_waits_for_confirmation(
    kronto: Kronto,
) -> None:
    """ТЗ §4, §7: отдел, выбранный самим сотрудником, сразу виден в профиле и
    в справочнике коллегам, но ждёт подтверждения (department_confirmed
    false)."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    boris = await join(kronto, company, "boris", "Борис")

    body = await pick_department(anna, world.dept_a)

    assert _dept_id(body) == world.dept_a, body
    assert body["department"]["name"] == "Бухгалтерия"
    assert body.get("department_confirmed") is False, body
    await assert_state(company, anna, world.dept_a, False)
    colleague_view = await directory_entry(boris.browser, anna.member_id)
    assert _dept_id(colleague_view) == world.dept_a, colleague_view
    assert colleague_view.get("department_confirmed") is False, colleague_view
    response = await boris.browser.get(f"{API}/people/{anna.member_id}")
    assert response.status_code == 200, response.text
    assert _dept_id(response.json()) == world.dept_a


async def test_unconfirmed_member_does_not_see_closed_folder(
    kronto: Kronto,
) -> None:
    """ТЗ §7: пока отдел не подтверждён, документов закрытых папок отдела
    сотрудник не видит — ни в списке документов, ни в ответах ассистента,
    ни в источниках. Обычные папки и документы без папки видны."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")

    await pick_department(anna, world.dept_a)

    await assert_hidden(anna.browser, world.secret_a, in_chat=True)
    await assert_visible(anna.browser, world.public, in_chat=True)
    await assert_visible(anna.browser, world.open_doc)
    # Контроль: администратор документ видит — он проиндексирован.
    await assert_admin_sees(company.admin, world.secret_a)


# ============================================================================
# Подтверждение
# ============================================================================


async def test_confirm_opens_closed_folder(kronto: Kronto) -> None:
    """ТЗ §7: администратор подтверждает отдел — с этого момента сотруднику
    открыта закрытая папка отдела."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    await pick_department(anna, world.dept_a)
    await assert_hidden(anna.browser, world.secret_a)

    response = await confirm(company.admin, anna)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == await anna.user_id()
    assert body.get("department_id") == world.dept_a, body
    assert body.get("department_confirmed") is True, body
    await assert_state(company, anna, world.dept_a, True)
    await assert_visible(anna.browser, world.secret_a, in_chat=True)
    await assert_hidden(anna.browser, world.secret_b)


async def test_confirm_is_for_the_department_the_admin_saw(kronto: Kronto) -> None:
    """ТЗ §7: подтверждают и отклоняют тот отдел, что видел администратор.
    Сотрудник успел сменить отдел — 409, новый отдел не подтверждается,
    закрытая папка закрыта."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    await pick_department(anna, world.dept_b)
    seen = (await user_row(company, await anna.user_id()))["department_id"]
    assert seen == world.dept_b

    await pick_department(anna, world.dept_a)
    confirmed = await confirm(company.admin, anna, seen)
    rejected = await reject(company.admin, anna, seen)

    assert confirmed.status_code == 409, confirmed.text
    assert rejected.status_code == 409, rejected.text
    await assert_state(company, anna, world.dept_a, False)
    await assert_hidden(anna.browser, world.secret_a)


async def test_repeated_confirm_returns_same_answer_without_changes(
    kronto: Kronto,
) -> None:
    """ТЗ §7 (openapi confirm): уже подтверждён — ответ тот же, без
    изменений; лишнего уведомления сотруднику нет."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    await pick_department(anna, world.dept_a)

    first = await confirm(company.admin, anna)
    second = await confirm(company.admin, anna)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    keys = ("id", "department_id", "department_confirmed", "role", "status")
    assert {k: second.json().get(k) for k in keys} == {
        k: first.json().get(k) for k in keys
    }
    assert second.json().get("department_confirmed") is True
    await assert_state(company, anna, world.dept_a, True)
    await assert_visible(anna.browser, world.secret_a)
    # допущение: «без изменений» — и без второго уведомления о решении
    assert len(await notifications(kronto, anna.browser, "department_confirmed")) == 1


async def test_confirm_admin_assigned_department_is_noop(kronto: Kronto) -> None:
    """ТЗ §7: отдел, назначенный админом, уже подтверждён; подтверждение
    ещё раз — тот же ответ, без изменений."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    await assign_department(company, anna.member_id, world.dept_a)

    response = await confirm(company.admin, anna)

    assert response.status_code == 200, response.text
    assert response.json().get("department_id") == world.dept_a
    assert response.json().get("department_confirmed") is True
    await assert_state(company, anna, world.dept_a, True)


async def test_confirm_without_department_is_conflict(kronto: Kronto) -> None:
    """ТЗ §7 (openapi confirm): подтверждать нечего — отдела нет — 409,
    ничего не меняется."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")

    response = await confirm(company.admin, anna)
    assert response.status_code == 409, response.text
    await assert_state(company, anna, None, False)

    # Выбрал и сам же убрал — снова нечего подтверждать.
    await pick_department(anna, world.dept_a)
    await pick_department(anna, None)
    response = await confirm(company.admin, anna)
    assert response.status_code == 409, response.text
    await assert_state(company, anna, None, False)
    await assert_hidden(anna.browser, world.secret_a)


# ============================================================================
# Отклонение
# ============================================================================


async def test_reject_removes_department(kronto: Kronto) -> None:
    """ТЗ §7: отклонить — отдел у человека снимается (department_id null),
    доступа к закрытой папке нет."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    await pick_department(anna, world.dept_a)

    response = await reject(company.admin, anna)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == await anna.user_id()
    assert body.get("department_id") is None, body
    assert body.get("department_confirmed") is False, body
    await assert_state(company, anna, None, False)
    await assert_hidden(anna.browser, world.secret_a)
    assert (await department_counts(company.admin, world.dept_a)) == (0, 0)


async def test_reject_without_department_is_conflict(kronto: Kronto) -> None:
    """ТЗ §7 (openapi reject): отдела нет — 409."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")

    response = await reject(company.admin, anna)

    assert response.status_code == 409, response.text
    await assert_state(company, anna, None, False)


async def test_reject_confirmed_department_is_conflict(kronto: Kronto) -> None:
    """ТЗ §7 (openapi reject): уже подтверждённый отдел не отклоняют — 409;
    отдел и доступ остаются (и назначенный админом, и подтверждённый)."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    boris = await join(kronto, company, "boris", "Борис")
    await assign_department(company, anna.member_id, world.dept_a)
    await pick_department(boris, world.dept_a)
    response = await confirm(company.admin, boris)
    assert response.status_code == 200, response.text

    for person in (anna, boris):
        response = await reject(company.admin, person)
        assert response.status_code == 409, response.text
        await assert_state(company, person, world.dept_a, True)
        await assert_visible(person.browser, world.secret_a)


# ============================================================================
# Смена, повтор и снятие отдела сотрудником
# ============================================================================


async def test_switching_department_drops_old_access_immediately(
    kronto: Kronto,
) -> None:
    """ТЗ §7: сотрудник выбрал другой отдел — новый ждёт подтверждения, а
    доступ по прежнему отделу пропадает сразу."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    await assign_department(company, anna.member_id, world.dept_a)
    await assert_visible(anna.browser, world.secret_a)

    body = await pick_department(anna, world.dept_b)

    assert _dept_id(body) == world.dept_b, body
    assert body.get("department_confirmed") is False, body
    await assert_state(company, anna, world.dept_b, False)
    await assert_hidden(anna.browser, world.secret_a, in_chat=True)
    await assert_hidden(anna.browser, world.secret_b)

    response = await confirm(company.admin, anna)
    assert response.status_code == 200, response.text
    await assert_visible(anna.browser, world.secret_b)
    await assert_hidden(anna.browser, world.secret_a)


async def test_switching_from_self_confirmed_department_also_waits(
    kronto: Kronto,
) -> None:
    """ТЗ §7: подтверждение относится к отделу, а не к человеку: после смены
    отдела прежнее подтверждение не переносится на новый."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    await pick_department(anna, world.dept_a)
    response = await confirm(company.admin, anna)
    assert response.status_code == 200, response.text

    await pick_department(anna, world.dept_b)

    await assert_state(company, anna, world.dept_b, False)
    await assert_hidden(anna.browser, world.secret_b)
    await assert_hidden(anna.browser, world.secret_a)

    # Вернулся в прежний отдел — это снова «другой» отдел: ждёт заново.
    await pick_department(anna, world.dept_a)
    await assert_state(company, anna, world.dept_a, False)
    await assert_hidden(anna.browser, world.secret_a)


async def test_reselecting_same_confirmed_department_keeps_confirmation(
    kronto: Kronto,
) -> None:
    """ТЗ §7: выбрал тот же отдел, что уже стоит, — ничего не меняется
    (подтверждение сохраняется), запроса администраторам нет."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    boris = await join(kronto, company, "boris", "Борис")
    await assign_department(company, anna.member_id, world.dept_a)
    await pick_department(boris, world.dept_a)
    response = await confirm(company.admin, boris)
    assert response.status_code == 200, response.text
    requests_before = len(
        await notifications(kronto, company.admin, "department_request")
    )

    for person in (anna, boris):
        body = await pick_department(person, world.dept_a)
        assert _dept_id(body) == world.dept_a, body
        assert body.get("department_confirmed") is True, body
        await assert_state(company, person, world.dept_a, True)
        await assert_visible(person.browser, world.secret_a)

    # допущение: «ничего не меняется» — и нового запроса администратору нет
    requests_after = len(
        await notifications(kronto, company.admin, "department_request")
    )
    assert requests_after == requests_before
    assert (await department_counts(company.admin, world.dept_a)) == (2, 0)


async def test_reselecting_same_pending_department_stays_pending(
    kronto: Kronto,
) -> None:
    """ТЗ §7: повторный выбор того же отдела, который ждёт подтверждения,
    ничего не меняет: по-прежнему ждёт, доступа нет, второго запроса нет."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    await pick_department(anna, world.dept_a)
    requests_before = len(
        await notifications(kronto, company.admin, "department_request")
    )

    body = await pick_department(anna, world.dept_a)

    assert body.get("department_confirmed") is False, body
    await assert_state(company, anna, world.dept_a, False)
    await assert_hidden(anna.browser, world.secret_a)
    # допущение: повтор того же выбора не плодит запросы администратору
    requests_after = len(
        await notifications(kronto, company.admin, "department_request")
    )
    assert requests_after == requests_before
    assert (await department_counts(company.admin, world.dept_a)) == (1, 1)


async def test_changing_position_keeps_department_confirmation(
    kronto: Kronto,
) -> None:
    """ТЗ §4, §7: должность меняется отдельно от отдела; правка должности не
    сбрасывает подтверждение отдела (не пришедшее поле не трогается)."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    await pick_department(anna, world.dept_a)
    response = await confirm(company.admin, anna)
    assert response.status_code == 200, response.text

    response = await anna.browser.patch(
        f"{API}/people/{anna.member_id}", json={"position": "Бухгалтер"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["position"] == "Бухгалтер"
    await assert_state(company, anna, world.dept_a, True)
    await assert_visible(anna.browser, world.secret_a)


async def test_clearing_department_is_immediate(kronto: Kronto) -> None:
    """ТЗ §7: убрал отдел — сразу, без подтверждения; доступ к закрытой
    папке пропадает сразу."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    await assign_department(company, anna.member_id, world.dept_a)
    await assert_visible(anna.browser, world.secret_a)

    body = await pick_department(anna, None)

    assert body.get("department") is None, body
    assert body.get("department_confirmed") is False, body
    await assert_state(company, anna, None, False)
    await assert_hidden(anna.browser, world.secret_a)
    assert (await department_counts(company.admin, world.dept_a)) == (0, 0)
    # Снимать нечего: ни подтверждать, ни отклонять.
    assert (await confirm(company.admin, anna)).status_code == 409
    assert (await reject(company.admin, anna)).status_code == 409


async def test_admin_can_clear_or_change_employee_department(
    kronto: Kronto,
) -> None:
    """ТЗ §4, §7: админ может поправить отдел сотрудника: назначенный им
    новый отдел подтверждён сразу, снятый — снят сразу."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    await pick_department(anna, world.dept_a)

    body = await assign_department(company, anna.member_id, world.dept_b)
    assert body.get("department_confirmed") is True, body
    await assert_state(company, anna, world.dept_b, True)
    await assert_visible(anna.browser, world.secret_b)
    await assert_hidden(anna.browser, world.secret_a)

    body = await assign_department(company, anna.member_id, None)
    assert body.get("department") is None, body
    await assert_state(company, anna, None, False)
    await assert_hidden(anna.browser, world.secret_b)


# ============================================================================
# Исходная дыра: папку открыли отделу уже после выбора
# ============================================================================


async def test_folder_opened_later_stays_closed_for_unconfirmed(
    kronto: Kronto,
) -> None:
    """ТЗ §7: сотрудники сами выбрали отдел без закрытых папок; позже
    админ открывает отделу закрытую папку — неподтверждённые её не видят,
    пока админ их не подтвердит. Назначенный админом видит сразу."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    boris = await join(kronto, company, "boris", "Борис")
    await pick_department(anna, world.dept_c)
    await assign_department(company, boris.member_id, world.dept_c)
    # Отдел без закрытых папок — запроса администраторам нет.
    assert await notifications(kronto, company.admin, "department_request") == []

    folder = await create_folder(
        company.admin,
        "Маркетинг — закрыто",
        restricted=True,
        departments=[world.dept_c],
    )
    later = Doc(
        await add_document(company.admin, "Бюджет", LATER_TEXT, folder),
        folder,
        LATER_QUESTION,
        "4417",
    )
    await index_all(kronto, company.admin, later)

    assert (await department_counts(company.admin, world.dept_c)) == (2, 1)
    await assert_visible(boris.browser, later)
    await assert_hidden(anna.browser, later, in_chat=True)

    response = await confirm(company.admin, anna)
    assert response.status_code == 200, response.text
    await assert_visible(anna.browser, later)


async def test_open_folder_restricted_later_hides_from_unconfirmed(
    kronto: Kronto,
) -> None:
    """ТЗ §5, §7: обычную папку админ делает закрытой и открывает отделу —
    сотрудник, сам выбравший этот отдел, перестаёт видеть её документы до
    подтверждения."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    await pick_department(anna, world.dept_c)
    await assert_visible(anna.browser, world.open_doc)

    response = await company.admin.patch(
        f"{API}/folders/{world.open_folder}",
        json={"restricted": True, "department_ids": [world.dept_c]},
    )
    assert response.status_code == 200, response.text

    await assert_hidden(anna.browser, world.open_doc)
    response = await confirm(company.admin, anna)
    assert response.status_code == 200, response.text
    await assert_visible(anna.browser, world.open_doc)


async def test_department_added_to_closed_folder_later_needs_confirmation(
    kronto: Kronto,
) -> None:
    """ТЗ §7: закрытую папку отдела Б админ открывает ещё и отделу В —
    неподтверждённый сотрудник отдела В её не видит."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    await pick_department(anna, world.dept_c)

    response = await company.admin.patch(
        f"{API}/folders/{world.folder_b}",
        json={"department_ids": [world.dept_b, world.dept_c]},
    )
    assert response.status_code == 200, response.text

    await assert_hidden(anna.browser, world.secret_b)
    response = await confirm(company.admin, anna)
    assert response.status_code == 200, response.text
    await assert_visible(anna.browser, world.secret_b)


# ============================================================================
# Администраторы
# ============================================================================


async def test_admin_choosing_own_department_is_confirmed(kronto: Kronto) -> None:
    """ТЗ §7: отдел, который администратор назначил себе, подтверждён
    сразу; запроса на подтверждение нет."""
    world = await build_world(kronto)
    company = world.company

    body = await assign_department(company, company.admin_member_id, world.dept_b)

    assert _dept_id(body) == world.dept_b, body
    assert body.get("department_confirmed") is True, body
    mine = await my_company(company.admin)
    assert _dept_id(mine) == world.dept_b
    assert mine.get("department_confirmed") is True, mine
    row = await user_row(company, await company.admin_user_id())
    assert row.get("department_confirmed") is True, row
    assert (await department_counts(company.admin, world.dept_b)) == (1, 0)
    assert await notifications(kronto, company.admin, "department_request") == []


async def test_admins_see_all_closed_folders_regardless_of_department(
    kronto: Kronto,
) -> None:
    """ТЗ §7: администраторам открыты все папки, как и раньше — и без
    отдела, и с другим отделом."""
    world = await build_world(kronto)
    company = world.company

    await assert_admin_sees(company.admin, world.secret_a)
    await assert_admin_sees(company.admin, world.secret_b)

    await assign_department(company, company.admin_member_id, world.dept_c)

    await assert_admin_sees(company.admin, world.secret_a)
    await assert_admin_sees(company.admin, world.secret_b)


# ============================================================================
# Права: кто может подтверждать и отклонять
# ============================================================================


async def test_employee_cannot_confirm_or_reject(kronto: Kronto) -> None:
    """ТЗ §7: подтверждает и отклоняет администратор. Сотрудник — ни свой
    отдел, ни коллеги (даже подтверждённый коллега того же отдела);
    ничего не меняется. Чужой отдел сотрудник не меняет (§4)."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    boris = await join(kronto, company, "boris", "Борис")
    await pick_department(anna, world.dept_a)
    await assign_department(company, boris.member_id, world.dept_a)

    attempts = [
        await confirm(anna.browser, anna),
        await confirm(boris.browser, anna),
        await reject(anna.browser, anna),
        await reject(boris.browser, anna),
    ]

    for response in attempts:
        # допущение: отказ не-администратору — 403 (или 404, не раскрывая)
        assert response.status_code in (403, 404), response.text
    await assert_state(company, anna, world.dept_a, False)
    await assert_hidden(anna.browser, world.secret_a)

    response = await set_department(boris.browser, anna.member_id, world.dept_b)
    # допущение: чужой профиль сотруднику не изменить — 403 или 404
    assert response.status_code in (403, 404), response.text
    await assert_state(company, anna, world.dept_a, False)


async def test_admin_of_other_company_cannot_confirm_or_reject(
    kronto: Kronto,
) -> None:
    """ТЗ §2, §7: решение по отделу — только администратор своей компании;
    админ чужой компании получает 404 или отказ, ничего не меняется."""
    world = await build_world(kronto)
    company = world.company
    other = await new_company(kronto, "vasilek")
    anna = await join(kronto, company, "anna", "Анна")
    boris = await join(kronto, company, "boris", "Борис")
    await pick_department(anna, world.dept_a)
    await pick_department(boris, world.dept_a)

    response = await confirm(other.admin, anna)
    # допущение: openapi — «иначе 404»; отказ 403 тоже не раскрывает данных
    assert response.status_code in (403, 404), response.text
    response = await reject(other.admin, boris)
    assert response.status_code in (403, 404), response.text

    await assert_state(company, anna, world.dept_a, False)
    await assert_state(company, boris, world.dept_a, False)
    await assert_hidden(anna.browser, world.secret_a)
    assert (await department_counts(company.admin, world.dept_a)) == (2, 2)


async def test_confirm_or_reject_unknown_or_inactive_person_is_404(
    kronto: Kronto,
) -> None:
    """ТЗ §7 (openapi): только работающий человек своей компании, иначе 404 —
    неизвестный id, заблокированный, убранный из компании."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    boris = await join(kronto, company, "boris", "Борис")
    await pick_department(anna, world.dept_a)
    await pick_department(boris, world.dept_a)

    anna_id = await anna.user_id()
    boris_id = await boris.user_id()

    unknown = str(uuid.uuid4())
    assert (await confirm(company.admin, unknown)).status_code == 404
    assert (await reject(company.admin, unknown)).status_code == 404

    response = await company.admin.patch(
        f"{API}/users/{anna_id}", json={"blocked": True}
    )
    assert response.status_code == 200, response.text
    assert (await confirm(company.admin, anna_id)).status_code == 404
    assert (await reject(company.admin, anna_id)).status_code == 404

    response = await company.admin.delete(f"{API}/users/{boris_id}")
    assert response.status_code == 204, response.text
    assert (await confirm(company.admin, boris_id)).status_code == 404
    assert (await reject(company.admin, boris_id)).status_code == 404

    # Заблокированному отдел не подтвердили.
    row = await user_row(company, anna_id)
    assert row.get("department_confirmed") is False, row


async def test_confirm_and_reject_require_login(kronto: Kronto) -> None:
    """ТЗ §3, §7: без входа подтверждать и отклонять нельзя — 401."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    await pick_department(anna, world.dept_a)
    guest = kronto.browser()

    assert (await confirm(guest, anna)).status_code == 401
    assert (await reject(guest, anna)).status_code == 401
    await assert_state(company, anna, world.dept_a, False)


# ============================================================================
# Уведомления и письма (§8)
# ============================================================================


async def test_department_request_notifies_every_admin(kronto: Kronto) -> None:
    """ТЗ §7, §8: выбранному отделу уже открыта закрытая папка — каждому
    администратору компании приходит «ждёт подтверждения» в колокольчик.
    Самому сотруднику и админу чужой компании — нет. Подтвердить может
    любой администратор компании."""
    world = await build_world(kronto)
    company = world.company
    other = await new_company(kronto, "vasilek")
    vera = await join(kronto, company, "vera", "Вера")
    second_admin = await promote_to_admin(kronto, company, vera)
    anna = await join(kronto, company, "anna", "Анна")

    await pick_department(anna, world.dept_a)

    for admin in (company.admin, second_admin):
        requests = await notifications(kronto, admin, "department_request")
        assert len(requests) >= 1, requests
        # допущение: один выбор — одно уведомление каждому администратору
        assert len(requests) == 1, requests
        assert requests[0]["read"] is False
    assert await notifications(kronto, anna.browser, "department_request") == []
    assert await notifications(kronto, other.admin, "department_request") == []

    response = await confirm(second_admin, anna)
    assert response.status_code == 200, response.text
    await assert_state(company, anna, world.dept_a, True)
    await assert_visible(anna.browser, world.secret_a)


async def test_department_request_even_for_empty_closed_folder(
    kronto: Kronto,
) -> None:
    """ТЗ §7: уведомление — если отделу открыта хотя бы одна закрытая папка;
    есть ли в ней уже документы, не важно."""
    world = await build_world(kronto)
    company = world.company
    await create_folder(
        company.admin, "Маркетинг — пусто", restricted=True, departments=[world.dept_c]
    )
    anna = await join(kronto, company, "anna", "Анна")

    await pick_department(anna, world.dept_c)

    assert len(await notifications(kronto, company.admin, "department_request")) == 1


async def test_no_department_request_for_department_without_closed_folder(
    kronto: Kronto,
) -> None:
    """ТЗ §7: выбор отдела без закрытых папок уведомлений не шлёт — такой
    человек просто в списке ждущих подтверждения."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    inbox_before = len(await kronto.inbox(company.admin_email))

    await pick_department(anna, world.dept_c)

    assert await notifications(kronto, company.admin, "department_request") == []
    assert len(await kronto.inbox(company.admin_email)) == inbox_before
    row = await user_row(company, await anna.user_id())
    assert row.get("department_id") == world.dept_c
    assert row.get("department_confirmed") is False, row
    assert (await department_counts(company.admin, world.dept_c)) == (1, 1)


async def test_department_request_email_follows_join_request_setting(
    kronto: Kronto,
) -> None:
    """ТЗ §7, §8: письмо администратору о «ждёт подтверждения» — по
    настройке писем о заявках; колокольчик приходит всегда."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    boris = await join(kronto, company, "boris", "Борис")

    await set_join_request_emails(company.admin, True)
    before = len(await kronto.inbox(company.admin_email))
    await pick_department(anna, world.dept_a)
    letters = (await kronto.inbox(company.admin_email))[before:]
    # допущение: текст письма ТЗ не задаёт — проверяем, что оно пришло
    assert len(letters) >= 1, letters
    assert len(await notifications(kronto, company.admin, "department_request")) == 1

    await set_join_request_emails(company.admin, False)
    before = len(await kronto.inbox(company.admin_email))
    await pick_department(boris, world.dept_b)
    assert (await kronto.inbox(company.admin_email))[before:] == []
    assert len(await notifications(kronto, company.admin, "department_request")) == 2


async def test_employee_gets_bell_but_no_email_about_decision(
    kronto: Kronto,
) -> None:
    """ТЗ §7, §8: о решении по отделу сотруднику — уведомление в
    колокольчике (подтверждён / отклонён), письма нет."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    await pick_department(anna, world.dept_a)
    letters_before = len(await kronto.inbox(anna.email))

    response = await confirm(company.admin, anna)
    assert response.status_code == 200, response.text

    confirmed = await notifications(kronto, anna.browser, "department_confirmed")
    assert len(confirmed) == 1, confirmed
    assert await notifications(kronto, anna.browser, "department_rejected") == []
    assert len(await kronto.inbox(anna.email)) == letters_before

    await pick_department(anna, world.dept_b)
    response = await reject(company.admin, anna)
    assert response.status_code == 200, response.text

    rejected = await notifications(kronto, anna.browser, "department_rejected")
    assert len(rejected) == 1, rejected
    assert len(await kronto.inbox(anna.email)) == letters_before
    # Решение — сотруднику, а не администратору.
    assert await notifications(kronto, company.admin, "department_confirmed") == []
    assert await notifications(kronto, company.admin, "department_rejected") == []


# ============================================================================
# Список отделов и «Люди»
# ============================================================================


async def test_department_list_counts_members_and_unconfirmed(
    kronto: Kronto,
) -> None:
    """ТЗ §7: в списке отделов, кроме числа людей, — сколько из них ждут
    подтверждения; числа следуют за выбором, подтверждением, отклонением
    и снятием отдела."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    boris = await join(kronto, company, "boris", "Борис")
    a, b = world.dept_a, world.dept_b

    assert await department_counts(company.admin, a) == (0, 0)
    await pick_department(anna, a)
    assert await department_counts(company.admin, a) == (1, 1)
    await assign_department(company, boris.member_id, a)
    assert await department_counts(company.admin, a) == (2, 1)
    assert (await confirm(company.admin, anna)).status_code == 200
    assert await department_counts(company.admin, a) == (2, 0)
    await pick_department(anna, b)
    assert await department_counts(company.admin, a) == (1, 0)
    assert await department_counts(company.admin, b) == (1, 1)
    assert (await reject(company.admin, anna)).status_code == 200
    assert await department_counts(company.admin, b) == (0, 0)
    await pick_department(boris, None)
    assert await department_counts(company.admin, a) == (0, 0)
    await assign_department(company, company.admin_member_id, b)
    assert await department_counts(company.admin, b) == (1, 0)


async def test_people_list_shows_who_waits_for_confirmation(
    kronto: Kronto,
) -> None:
    """ТЗ §7: в «Сотрудниках» админ видит, кто ждёт подтверждения отдела:
    у каждого — department_id и department_confirmed."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    boris = await join(kronto, company, "boris", "Борис")
    await pick_department(anna, world.dept_a)
    await assign_department(company, boris.member_id, world.dept_a)

    response = await company.admin.get(f"{API}/users")

    assert response.status_code == 200, response.text
    rows = {row["id"]: row for row in response.json()}
    for row in rows.values():
        assert "department_id" in row, row
        assert "department_confirmed" in row, row
    anna_row = rows[await anna.user_id()]
    boris_row = rows[await boris.user_id()]
    admin_row = rows[await company.admin_user_id()]
    assert anna_row["department_id"] == world.dept_a
    assert anna_row["department_confirmed"] is False
    assert boris_row["department_id"] == world.dept_a
    assert boris_row["department_confirmed"] is True
    assert admin_row["department_id"] is None
    assert admin_row["department_confirmed"] is False


# ============================================================================
# Общий диалог (§6)
# ============================================================================


async def test_shared_dialog_hides_closed_folder_from_unconfirmed(
    kronto: Kronto,
) -> None:
    """ТЗ §6, §7: диалог с ответом по документу закрытой папки, которым
    поделился подтверждённый коллега, не раскрывает ни документа, ни
    ответа неподтверждённому сотруднику того же отдела."""
    world = await build_world(kronto)
    company = world.company
    boris = await join(kronto, company, "boris", "Борис")
    anna = await join(kronto, company, "anna", "Анна")
    await assign_department(company, boris.member_id, world.dept_a)
    conversation_id, answer = await chat(boris.browser, world.secret_a.question)
    assert world.secret_a.code in answer["content"], answer
    response = await boris.browser.post(f"{API}/conversations/{conversation_id}/share")
    assert response.status_code == 200, response.text
    token = response.json()["token"]

    await pick_department(anna, world.dept_a)
    response = await open_shared(anna.browser, token)

    if response.status_code == 200:
        assert world.secret_a.code not in response.text, response.text
        for message in response.json()["messages"]:
            for source in message["sources"]:
                if source.get("material_id") == world.secret_a.material_id:
                    assert source["content"] is None, source
    else:
        # допущение: диалог можно и не показать целиком
        assert response.status_code in (403, 404), response.text

    # Контроль: после подтверждения тот же диалог показывает ответ.
    assert (await confirm(company.admin, anna)).status_code == 200
    response = await open_shared(anna.browser, token)
    assert response.status_code == 200, response.text
    assert world.secret_a.code in response.text


# ============================================================================
# Журнал
# ============================================================================


async def test_confirm_and_reject_are_recorded_in_journal(kronto: Kronto) -> None:
    """ТЗ §7: подтверждение и отклонение отдела — в журнале."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    boris = await join(kronto, company, "boris", "Борис")
    await pick_department(anna, world.dept_a)
    await pick_department(boris, world.dept_b)
    targets = {anna.member_id, boris.member_id}
    targets |= {await anna.user_id(), await boris.user_id()}

    seen = {e["id"] for e in await audit_events(company.admin)}
    assert (await confirm(company.admin, anna)).status_code == 200
    confirmed = [e for e in await audit_events(company.admin) if e["id"] not in seen]

    seen |= {e["id"] for e in confirmed}
    assert (await reject(company.admin, boris)).status_code == 200
    rejected = [e for e in await audit_events(company.admin) if e["id"] not in seen]

    # допущение: названия действий ТЗ не задаёт — запись появилась, она про
    # отдел или про этого человека, а подтверждение и отклонение различимы
    for events in (confirmed, rejected):
        assert events, "решение по отделу не попало в журнал"
        assert any(
            "depart" in e["action"].lower() or e.get("target_id") in targets
            for e in events
        ), events
    confirm_marks = {(e["action"], json.dumps(e["details"])) for e in confirmed}
    reject_marks = {(e["action"], json.dumps(e["details"])) for e in rejected}
    assert confirm_marks != reject_marks


# ============================================================================
# Удаление отдела
# ============================================================================


async def test_deleted_department_removes_it_with_confirmation(
    kronto: Kronto,
) -> None:
    """ТЗ §7: отдел удалён — у людей он снимается вместе с подтверждением;
    выбранный потом другой отдел снова ждёт подтверждения."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    await assign_department(company, anna.member_id, world.dept_c)
    await assert_state(company, anna, world.dept_c, True)

    response = await company.admin.delete(f"{API}/departments/{world.dept_c}")

    assert response.status_code == 204, response.text
    await assert_state(company, anna, None, False)
    listed = (await company.admin.get(f"{API}/departments")).json()
    assert world.dept_c not in {d["id"] for d in listed}

    await pick_department(anna, world.dept_a)
    await assert_state(company, anna, world.dept_a, False)
    await assert_hidden(anna.browser, world.secret_a)


async def test_deleting_department_closes_its_folder_for_members(
    kronto: Kronto,
) -> None:
    """ТЗ §7: удалили отдел, которому была открыта закрытая папка (вместе с
    другим отделом), — бывшие его люди теряют доступ сразу, подтверждение
    не переходит на новый отдел."""
    world = await build_world(kronto)
    company = world.company
    anna = await join(kronto, company, "anna", "Анна")
    response = await company.admin.patch(
        f"{API}/folders/{world.folder_b}",
        json={"department_ids": [world.dept_b, world.dept_c]},
    )
    assert response.status_code == 200, response.text
    await assign_department(company, anna.member_id, world.dept_c)
    await assert_visible(anna.browser, world.secret_b)

    response = await company.admin.delete(f"{API}/departments/{world.dept_c}")

    # допущение: отдел, привязанный к папке вместе с другим отделом, удалить
    # можно (ТЗ запрета не ставит, документы папки при этом не открываются)
    assert response.status_code == 204, response.text
    await assert_state(company, anna, None, False)
    await assert_hidden(anna.browser, world.secret_b)
    await pick_department(anna, world.dept_b)
    await assert_state(company, anna, world.dept_b, False)
    await assert_hidden(anna.browser, world.secret_b)


# ============================================================================
# Разделение компаний
# ============================================================================


async def test_department_of_another_company_is_rejected(kronto: Kronto) -> None:
    """ТЗ §2, §4, §7: отдел чужой компании (или несуществующий) не выбрать
    ни сотруднику, ни администратору, не привязать к папке; отдел
    человека не меняется."""
    world = await build_world(kronto)
    company = world.company
    other = await new_company(kronto, "vasilek")
    foreign = await create_department(other.admin, "Чужая бухгалтерия")
    anna = await join(kronto, company, "anna", "Анна")
    await pick_department(anna, world.dept_a)

    for department_id in (foreign, str(uuid.uuid4())):
        response = await set_department(anna.browser, anna.member_id, department_id)
        # допущение: код отказа ТЗ не задаёт — любой 4xx
        assert 400 <= response.status_code < 500, response.text
        response = await set_department(company.admin, anna.member_id, department_id)
        assert 400 <= response.status_code < 500, response.text
        await assert_state(company, anna, world.dept_a, False)

    response = await company.admin.post(
        f"{API}/folders",
        json={"name": "Чужое", "restricted": True, "department_ids": [foreign]},
    )
    assert 400 <= response.status_code < 500, response.text
    listed = (await company.admin.get(f"{API}/departments")).json()
    assert foreign not in {d["id"] for d in listed}
    assert await department_counts(other.admin, foreign) == (0, 0)
