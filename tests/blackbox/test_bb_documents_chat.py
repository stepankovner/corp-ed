"""Чёрный ящик: документы, папки по отделам, ответы и чат (ТЗ §5–§7).

Тесты написаны только по ТЗ и схеме API, без чтения кода продукта.
Как обращаться к API — из схемы; что проверять — из ТЗ. Где ТЗ молчит о
детали (точный код ошибки, текст), проверяется то, что обязано
выполняться при любом разумном решении, с пометкой «# допущение: …».

Особенности тестовой среды (HARNESS.md), на которые опираются тесты:
- поиск — по общим словам в той же форме; в каждом тесте посылка
  «у вопроса есть / нет общих слов с документом» проверяется явно
  (assert_shares_words / assert_no_shared_words), чтобы ошибка автора
  теста не выглядела как ошибка продукта;
- модель-заглушка отвечает «Режим разработки, ответ без модели. По
  документам: <начало выдержки> [1]» или «Режим разработки: общий ответ
  без модели.»; поэтому слово-метка ставится первым словом документа;
- документ ищется только после `await kronto.run_background()`.
"""

from __future__ import annotations

import base64
import io
import json
import math
import re
import uuid
import zipfile
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from tests.blackbox.conftest import API, Kronto

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

TEMP_PASSWORD = "Vremennyi-Parol-Kronto-2026!"
ADMIN_PASSWORD = "Novyi-Nadezhnyi-Parol-Kronto-2026!"
EMPLOYEE_PASSWORD = "Sotrudnik-Parol-Kronto-2026!"

# допущение: отказ «нет доступа» — 403 или 404 (скрыть само существование);
# ТЗ код не задаёт.
DENIED = (403, 404)
# допущение: без входа — 401 (или 403 у схемы HTTPBearer без заголовка).
UNAUTHENTICATED = (401, 403)

# Тексты модели-заглушки (HARNESS.md).
STUB_DOCS = "Режим разработки, ответ без модели. По документам:"
STUB_GENERAL = "общий ответ без модели"
# Пометка «ответа в документах нет» (схема: GENERAL_ANSWER_PREFIX и
# NOT_FOUND_ANSWER начинаются с этой фразы).
NOT_FOUND_PREFIX = "В документах компании ответа нет"

# PNG 1×1 — картинка, не документ.
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
# Исполняемый файл Windows (сигнатура MZ) — тоже не документ.
EXE_BYTES = (
    b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00"
    + bytes(range(256)) * 4
)

# Закрытый документ отдела и общий документ (используются в нескольких тестах).
SECRET_TITLE = "Бирюза — сейф"
SECRET_CONTENT = "Бирюза. Код сейфа бухгалтерии: 7931. Ключ хранит главный бухгалтер."
SECRET_QUESTION = "Какой код сейфа бухгалтерии?"
SECRET_MARKERS = ("Бирюза", "7931")
COMMON_TITLE = "Столовая"
COMMON_CONTENT = "Столовая открыта с полудня, обед подают до трёх часов."
COMMON_QUESTION = "Когда открыта столовая?"
COMMON_MARKER = "полудня"
FOLDER_NAME = "Папка Гранат"


# ---------------------------------------------------------------------------
# Проверка посылок теста
# ---------------------------------------------------------------------------


def words(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"\w+", text)}


def assert_shares_words(question: str, *texts: str) -> None:
    common = words(question) & words(" ".join(texts))
    assert common, (
        f"тест составлен неверно: у вопроса {question!r} нет общих слов с документом"
    )


def assert_no_shared_words(question: str, *texts: str) -> None:
    common = words(question) & words(" ".join(texts))
    assert not common, (
        f"тест составлен неверно: у вопроса {question!r} общие слова {common}"
    )


def assert_no_leak(text: str, *needles: str) -> None:
    for needle in needles:
        assert needle not in text, f"утечка {needle!r}: {text[:3000]}"


# ---------------------------------------------------------------------------
# Сеанс человека в браузере
# ---------------------------------------------------------------------------


@dataclass
class Session:
    """Вошедший человек: свой браузер (cookie) и access-токен для Authorization."""

    browser: httpx.AsyncClient
    token: str
    email: str
    first_name: str = ""
    last_name: str = ""
    tenant_id: str = ""
    member_id: str = ""

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token}"}
        headers.update(kwargs.pop("headers", None) or {})
        return await self.browser.request(method, url, headers=headers, **kwargs)

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("PUT", url, **kwargs)

    async def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("PATCH", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("DELETE", url, **kwargs)

    async def me(self) -> dict[str, Any]:
        r = await self.get(f"{API}/auth/me")
        assert r.status_code == 200, r.text
        me = r.json()
        company = me.get("company") or {}
        self.tenant_id = company.get("tenant_id") or ""
        self.member_id = company.get("member_id") or ""
        return me


async def login_with_totp(kronto: Kronto, email: str, password: str) -> Session:
    """Полный вход администратора: пароль + приложение-аутентификатор (ТЗ §3)."""
    secret = await kronto.enable_totp(email)
    browser = kronto.browser()
    r = await browser.post(
        f"{API}/auth/login", json={"email": email, "password": password}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    if body["status"] == "ok":
        token = body["access_token"]
    else:
        assert body["status"] == "mfa_required", body
        assert "totp" in body["mfa"]["methods"], body
        r = await browser.post(
            f"{API}/auth/mfa/verify",
            json={
                "method": "totp",
                "code": kronto.totp(secret),
                "token": body["mfa"]["token"],
            },
        )
        assert r.status_code == 200, r.text
        token = r.json()["access_token"]
    session = Session(browser=browser, token=token, email=email)
    me = await session.me()
    if me["must_change_password"]:
        r = await session.post(
            f"{API}/auth/change-password",
            json={"current_password": password, "new_password": ADMIN_PASSWORD},
        )
        assert r.status_code == 200, r.text
        session.token = r.json()["access_token"]
        me = await session.me()
    session.first_name = me.get("first_name") or ""
    session.last_name = me.get("last_name") or ""
    assert session.tenant_id, me
    return session


async def make_admin(kronto: Kronto, code: str, **company_kwargs: Any) -> Session:
    """Компания и её вошедший администратор."""
    email = f"admin@{code}-corp.ru"
    await kronto.create_company(
        code=code,
        name=f"Компания {code.capitalize()}",
        admin_email=email,
        admin_password=TEMP_PASSWORD,
        **company_kwargs,
    )
    admin = await login_with_totp(kronto, email, TEMP_PASSWORD)
    me = await admin.me()
    assert me["company"]["role"] == "admin", me
    return admin


def _code_from_letter(text: str) -> str:
    codes = re.findall(r"\b\d{6}\b", text)
    if not codes:
        codes = [
            c.replace(" ", "").replace("-", "")
            for c in re.findall(r"\b\d{3}[ -]\d{3}\b", text)
        ]
    assert codes, f"в письме нет кода из 6 цифр: {text}"
    return codes[0]


async def create_invite(admin: Session, **kwargs: Any) -> dict[str, Any]:
    r = await admin.post(f"{API}/invites", json=kwargs)
    assert r.status_code == 201, r.text
    return r.json()


async def join_employee(
    kronto: Kronto,
    admin: Session,
    email: str,
    first_name: str = "Иван",
    last_name: str = "Петров",
    department_id: str | None = None,
) -> Session:
    """Регистрация (ТЗ §2) → подтверждение почты кодом (ТЗ §3) → вступление по
    приглашению администратора (ТЗ §2). При необходимости админ ставит отдел."""
    invite = await create_invite(admin)
    browser = kronto.browser()
    r = await browser.post(
        f"{API}/auth/register",
        json={
            "email": email,
            "password": EMPLOYEE_PASSWORD,
            "first_name": first_name,
            "last_name": last_name,
            "consent": True,
        },
    )
    assert r.status_code == 202, r.text
    letter = await kronto.last_letter(email)
    code = _code_from_letter(letter.text or letter.html or "")
    r = await browser.post(
        f"{API}/auth/verify-email", json={"email": email, "code": code}
    )
    assert r.status_code == 200, r.text
    session = Session(
        browser=browser,
        token=r.json()["access_token"],
        email=email,
        first_name=first_name,
        last_name=last_name,
    )
    r = await session.post(f"{API}/invites/accept", json={"secret": invite["token"]})
    assert r.status_code == 200, r.text
    join = r.json()
    assert join["outcome"] == "joined", join
    if join.get("session"):
        session.token = join["session"]["access_token"]
    me = await session.me()
    if session.tenant_id != admin.tenant_id:
        r = await session.post(
            f"{API}/auth/switch-company", json={"tenant_id": admin.tenant_id}
        )
        assert r.status_code == 200, r.text
        session.token = r.json()["access_token"]
        me = await session.me()
    assert session.tenant_id == admin.tenant_id, me
    assert me["company"]["role"] == "employee", me
    if department_id is not None:
        await set_department(admin, session.member_id, department_id)
    return session


async def make_department(admin: Session, name: str) -> str:
    r = await admin.post(f"{API}/departments", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def set_department(
    admin: Session, member_id: str, department_id: str | None
) -> None:
    r = await admin.patch(
        f"{API}/people/{member_id}", json={"department_id": department_id}
    )
    assert r.status_code == 200, r.text
    department = r.json()["department"]
    if department_id is None:
        assert department is None, r.text
    else:
        assert department is not None and department["id"] == department_id, r.text


async def make_folder(
    admin: Session, name: str, department_ids: list[str], restricted: bool = True
) -> dict[str, Any]:
    r = await admin.post(
        f"{API}/folders",
        json={"name": name, "restricted": restricted, "department_ids": department_ids},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def add_doc(
    admin: Session, title: str, content: str, folder_id: str | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {"title": title, "content": content}
    if folder_id is not None:
        payload["folder_id"] = folder_id
    r = await admin.post(f"{API}/materials", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


async def upload_doc(
    admin: Session,
    title: str,
    filename: str,
    data: bytes,
    content_type: str = "text/plain",
    folder_id: str | None = None,
) -> httpx.Response:
    form: dict[str, str] = {"title": title}
    if folder_id is not None:
        form["folder_id"] = folder_id
    return await admin.post(
        f"{API}/materials/upload",
        files={"file": (filename, data, content_type)},
        data=form,
    )


async def upload_attachment(
    s: Session, filename: str, data: bytes, content_type: str = "text/plain"
) -> httpx.Response:
    return await s.post(
        f"{API}/attachments", files={"file": (filename, data, content_type)}
    )


# ---------------------------------------------------------------------------
# Поток ответа (Server-Sent Events)
# ---------------------------------------------------------------------------


def sse_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in re.split(r"\r?\n\r?\n", text):
        name: str | None = None
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].lstrip())
        if not data_lines:
            continue
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            if "type" not in payload and name:
                payload["type"] = name
            events.append(payload)
    return events


@dataclass
class Reply:
    """Разобранный поток ответа: первым — start, последним — done или error (схема)."""

    response: httpx.Response
    events: list[dict[str, Any]]

    @property
    def text(self) -> str:
        return self.response.text

    @property
    def types(self) -> list[str]:
        return [e.get("type", "") for e in self.events]

    @property
    def start(self) -> dict[str, Any]:
        return self.events[0]

    @property
    def final(self) -> dict[str, Any]:
        return self.events[-1]

    @property
    def ok(self) -> bool:
        return self.final.get("type") == "done"

    @property
    def answer(self) -> dict[str, Any]:
        if self.final.get("type") == "done":
            return self.final["answer"]
        return self.final.get("answer") or self.start["answer"]

    @property
    def conversation_id(self) -> str:
        return self.start["conversation"]["id"]

    @property
    def question_id(self) -> str:
        return self.start["question"]["id"]

    @property
    def answer_id(self) -> str:
        return self.start["answer"]["id"]

    @property
    def origin(self) -> str | None:
        return self.answer.get("origin")

    @property
    def content(self) -> str:
        return self.answer.get("content") or ""

    @property
    def sources(self) -> list[dict[str, Any]]:
        return self.answer.get("sources") or []

    @property
    def material_ids(self) -> list[str | None]:
        return [src.get("material_id") for src in self.sources]


async def stream_post(
    s: Session, url: str, body: dict[str, Any] | None = None
) -> Reply:
    r = await s.post(url, json=body) if body is not None else await s.post(url)
    assert r.status_code == 200, r.text
    assert r.headers.get("content-type", "").startswith("text/event-stream"), r.headers
    events = sse_events(r.text)
    assert events, f"пустой поток: {r.text!r}"
    assert events[0].get("type") == "start", events[0]
    assert events[-1].get("type") in ("done", "error"), events[-1]
    return Reply(response=r, events=events)


async def ask(
    s: Session,
    question: str,
    conversation_id: str | None = None,
    parent_id: str | None = None,
    attachment_ids: list[str] | None = None,
) -> Reply:
    """Новый диалог (conversation_id=None) или вопрос в диалоге после parent_id."""
    body: dict[str, Any] = {"question": question}
    if attachment_ids is not None:
        body["attachment_ids"] = attachment_ids
    if conversation_id is None:
        return await stream_post(s, f"{API}/conversations", body)
    body["parent_id"] = parent_id
    return await stream_post(s, f"{API}/conversations/{conversation_id}/messages", body)


async def regenerate(s: Session, conversation_id: str, question_id: str) -> Reply:
    return await stream_post(
        s, f"{API}/conversations/{conversation_id}/messages/{question_id}/regenerate"
    )


async def get_conversation(s: Session, conversation_id: str) -> dict[str, Any]:
    r = await s.get(f"{API}/conversations/{conversation_id}")
    assert r.status_code == 200, r.text
    return r.json()


async def list_conversations(s: Session, **params: Any) -> list[dict[str, Any]]:
    r = await s.get(f"{API}/conversations", params=params)
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def faq_ask(s: Session, question: str) -> httpx.Response:
    r = await s.post(f"{API}/faq/ask", json={"question": question})
    assert r.status_code == 200, r.text
    return r


async def get_usage(admin: Session) -> dict[str, Any]:
    r = await admin.get(f"{API}/usage")
    assert r.status_code == 200, r.text
    return r.json()


def make_docx(paragraphs: list[str]) -> bytes:
    """Минимальный .docx (Office Open XML) в порядке частей, как у Word."""
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
        '2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    document_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        "</Relationships>"
    )
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}<w:sectPr/></w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/_rels/document.xml.rels", document_rels)
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


@dataclass
class Restricted:
    """Компания с закрытой папкой отдела «Бухгалтерия» и общим документом."""

    admin: Session
    insider: Session | None
    outsider: Session | None
    dept_in: str
    dept_out: str
    folder_id: str
    secret_id: str
    common_id: str


async def setup_restricted(
    kronto: Kronto, code: str = "alfa", insider: bool = True, outsider: bool = True
) -> Restricted:
    assert_shares_words(SECRET_QUESTION, SECRET_CONTENT)
    assert_shares_words(COMMON_QUESTION, COMMON_CONTENT)
    assert_no_shared_words(SECRET_QUESTION, COMMON_TITLE, COMMON_CONTENT)
    assert_no_shared_words(COMMON_QUESTION, SECRET_TITLE, SECRET_CONTENT)

    admin = await make_admin(kronto, code)
    dept_in = await make_department(admin, "Бухгалтерия")
    dept_out = await make_department(admin, "Продажи")
    insider_session = None
    outsider_session = None
    if insider:
        insider_session = await join_employee(
            kronto,
            admin,
            f"buh@{code}-corp.ru",
            "Галина",
            "Счётова",
            department_id=dept_in,
        )
    if outsider:
        outsider_session = await join_employee(
            kronto,
            admin,
            f"sales@{code}-corp.ru",
            "Пётр",
            "Продажин",
            department_id=dept_out,
        )
    folder = await make_folder(admin, FOLDER_NAME, [dept_in], restricted=True)
    assert folder["restricted"] is True
    assert [d["id"] for d in folder["departments"]] == [dept_in]
    secret = await add_doc(admin, SECRET_TITLE, SECRET_CONTENT, folder_id=folder["id"])
    assert secret["folder_id"] == folder["id"]
    common = await add_doc(admin, COMMON_TITLE, COMMON_CONTENT)
    await kronto.run_background()
    return Restricted(
        admin=admin,
        insider=insider_session,
        outsider=outsider_session,
        dept_in=dept_in,
        dept_out=dept_out,
        folder_id=folder["id"],
        secret_id=secret["id"],
        common_id=common["id"],
    )


async def assert_secret_hidden_from(s: Session, secret_id: str) -> None:
    """Ни в потоке чата, ни в /faq/ask нет ни слова закрытого документа."""
    reply = await ask(s, SECRET_QUESTION)
    assert reply.origin != "documents", reply.answer
    assert secret_id not in reply.material_ids
    assert_no_leak(reply.text, secret_id, *SECRET_MARKERS)
    r = await faq_ask(s, SECRET_QUESTION)
    assert r.json()["origin"] != "documents", r.text
    assert_no_leak(r.text, secret_id, *SECRET_MARKERS)


async def assert_secret_visible_to(s: Session, secret_id: str) -> None:
    reply = await ask(s, SECRET_QUESTION)
    assert reply.ok, reply.final
    assert reply.origin == "documents", reply.answer
    assert secret_id in reply.material_ids, reply.sources
    assert SECRET_MARKERS[0] in reply.content, reply.content


# ===========================================================================
# Документы (ТЗ §5, §7)
# ===========================================================================


async def test_document_answers_only_after_indexing_and_cites_source(
    kronto: Kronto,
) -> None:
    """ТЗ §5, §6, §7: документ, загруженный администратором, становится источником
    ответа после индексации; ответ по документам — со ссылкой на источник
    (фрагмент и подпись — название документа)."""
    admin = await make_admin(kronto, "alfa")
    title = "Кедр: правила отпусков"
    content = "Кедр. Ежегодный отпуск длится двадцать восемь календарных дней."
    question = "Сколько длится ежегодный отпуск?"
    assert_shares_words(question, content)

    doc = await add_doc(admin, title, content)
    assert doc["title"] == title
    assert doc["status"] in ("pending", "processing"), doc

    before = await ask(admin, question)
    assert before.origin != "documents", before.answer
    assert doc["id"] not in before.material_ids

    await kronto.run_background()
    r = await admin.get(f"{API}/materials/{doc['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ready", r.text
    assert r.json()["indexed_at"] is not None

    after = await ask(admin, question)
    assert after.ok, after.final
    assert after.origin == "documents"
    assert STUB_DOCS in after.content, after.content
    assert "Кедр" in after.content
    source = next(src for src in after.sources if src["material_id"] == doc["id"])
    assert source["kind"] == "document"
    # схема: title материала уходит «в подписи источников».
    assert source["title"] == title
    assert source["content"] and "отпуск" in source["content"]


async def test_upload_txt_and_md_files_become_searchable(kronto: Kronto) -> None:
    """ТЗ §5, §7: загрузка файлов разрешённых форматов (txt, md) — документы
    попадают в базу компании и находятся по вопросу."""
    admin = await make_admin(kronto, "alfa")
    txt = "Ольха. Пропуск для гостей заказывает секретарь за сутки."
    md = "# Ясень\n\nЯсень. Велосипеды оставляют во дворе у западного входа.\n"
    q_txt = "Кто заказывает пропуск для гостей?"
    q_md = "Где оставляют велосипеды?"
    assert_shares_words(q_txt, txt)
    assert_shares_words(q_md, md)
    assert_no_shared_words(q_txt, md, "Ясень: велосипеды")
    assert_no_shared_words(q_md, txt, "Ольха: гости")

    r = await upload_doc(admin, "Ольха: гости", "gosti.txt", txt.encode(), "text/plain")
    assert r.status_code == 201, r.text
    txt_doc = r.json()
    assert txt_doc["title"] == "Ольха: гости"
    r = await upload_doc(
        admin, "Ясень: велосипеды", "velo.md", md.encode(), "text/markdown"
    )
    assert r.status_code == 201, r.text
    md_doc = r.json()

    await kronto.run_background()
    r = await admin.get(f"{API}/materials")
    assert r.status_code == 200, r.text
    listed = {m["id"]: m for m in r.json()}
    assert listed[txt_doc["id"]]["status"] == "ready"
    assert listed[md_doc["id"]]["status"] == "ready"
    assert listed[txt_doc["id"]]["source_filename"] == "gosti.txt"

    reply = await ask(admin, q_txt)
    assert reply.origin == "documents" and txt_doc["id"] in reply.material_ids
    assert "Ольха" in reply.content
    reply = await ask(admin, q_md)
    assert reply.origin == "documents" and md_doc["id"] in reply.material_ids


async def test_upload_docx_file_becomes_searchable(kronto: Kronto) -> None:
    """ТЗ §5, §7: документ Word (.docx — разрешённый формат по схеме) загружается
    и находится по вопросу."""
    admin = await make_admin(kronto, "alfa")
    text = "Платан. Командировочные выплачивает бухгалтерия до отъезда."
    question = "Кто выплачивает командировочные?"
    assert_shares_words(question, text)
    data = make_docx([text])
    r = await upload_doc(
        admin,
        "Платан: командировки",
        "komandirovki.docx",
        data,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert r.status_code == 201, r.text
    doc = r.json()
    await kronto.run_background()
    r = await admin.get(f"{API}/materials/{doc['id']}")
    assert r.json()["status"] == "ready", r.text
    reply = await ask(admin, question)
    assert reply.origin == "documents" and doc["id"] in reply.material_ids
    assert "Платан" in reply.content


async def test_upload_refuses_files_that_are_not_documents(kronto: Kronto) -> None:
    """ТЗ §5, §7: загружаются только документы (схема: docx, doc, xlsx, pptx, pdf,
    txt, md; формат — по содержимому, а не по расширению). Картинка и программа —
    отказ, в базу ничего не попадает."""
    admin = await make_admin(kronto, "alfa")
    cases = [
        ("photo.png", PNG_1X1, "image/png"),
        ("notes.txt", PNG_1X1, "text/plain"),  # картинка под видом текста
        ("tool.exe", EXE_BYTES, "application/octet-stream"),
        ("tool.pdf", EXE_BYTES, "application/pdf"),  # программа под видом pdf
    ]
    for filename, data, content_type in cases:
        r = await upload_doc(admin, "Не документ", filename, data, content_type)
        # допущение: отказ сразу при загрузке, код 4xx (какой именно — ТЗ не задаёт).
        assert 400 <= r.status_code < 500, (filename, r.status_code, r.text)

    await kronto.run_background()
    r = await admin.get(f"{API}/materials")
    assert r.status_code == 200, r.text
    assert all(m["title"] != "Не документ" for m in r.json()), r.text


async def test_document_input_validation(kronto: Kronto) -> None:
    """ТЗ §7 (документы): пустой и слишком длинный ввод отклоняется по схеме:
    название 1–200 символов, текст 1–200 000 символов, файл обязателен."""
    admin = await make_admin(kronto, "alfa")
    bad_payloads = [
        {"title": "Пусто", "content": ""},
        {"title": "", "content": "Текст документа."},
        {"title": "Т" * 201, "content": "Текст документа."},
        {"title": "Длинный", "content": "а" * 200_001},
        {"content": "Без названия."},
    ]
    for payload in bad_payloads:
        r = await admin.post(f"{API}/materials", json=payload)
        assert r.status_code == 422, (payload.get("title"), r.status_code)

    r = await upload_doc(admin, "   ", "space.txt", "Текст.".encode())
    assert r.status_code == 422, r.text
    r = await admin.post(f"{API}/materials/upload", data={"title": "Без файла"})
    assert r.status_code == 422, r.text

    r = await admin.get(f"{API}/materials")
    assert r.status_code == 200 and r.json() == [], r.text


async def test_employee_cannot_manage_documents_or_folders(kronto: Kronto) -> None:
    """ТЗ §2, §7: всё, что связано с документами и папками, делает администратор;
    сотрудник не создаёт, не меняет, не удаляет и не переиндексирует документы."""
    admin = await make_admin(kronto, "alfa")
    employee = await join_employee(kronto, admin, "ivan@alfa-corp.ru")
    content = "Сосна. Курьерская доставка работает по будням."
    question = "Когда работает курьерская доставка?"
    assert_shares_words(question, content)
    doc = await add_doc(admin, "Сосна: доставка", content)
    await kronto.run_background()

    r = await employee.post(
        f"{API}/materials", json={"title": "Моё", "content": "Мой текст."}
    )
    assert r.status_code in DENIED, r.text
    r = await upload_doc(employee, "Моё", "my.txt", "Мой текст.".encode())
    assert r.status_code in DENIED, r.text
    r = await employee.patch(f"{API}/materials/{doc['id']}", json={"title": "Сломано"})
    assert r.status_code in DENIED, r.text
    r = await employee.post(f"{API}/materials/{doc['id']}/ingest")
    assert r.status_code in DENIED, r.text
    r = await employee.delete(f"{API}/materials/{doc['id']}")
    assert r.status_code in DENIED, r.text
    r = await employee.post(f"{API}/folders", json={"name": "Моя папка"})
    assert r.status_code in DENIED, r.text

    r = await admin.get(f"{API}/materials/{doc['id']}")
    assert r.status_code == 200 and r.json()["title"] == "Сосна: доставка", r.text
    r = await admin.get(f"{API}/materials")
    assert [m["id"] for m in r.json()] == [doc["id"]]
    reply = await ask(employee, question)
    assert reply.origin == "documents" and doc["id"] in reply.material_ids


async def test_documents_and_chat_require_login(kronto: Kronto) -> None:
    """ТЗ §3, §6: без входа нет ни документов, ни диалогов, ни вопросов."""
    admin = await make_admin(kronto, "alfa")
    doc = await add_doc(admin, "Ель: охрана", "Ель. Охрана дежурит круглосуточно.")
    await kronto.run_background()
    guest = kronto.browser()
    checks = [
        ("GET", f"{API}/materials", None),
        ("GET", f"{API}/materials/{doc['id']}", None),
        ("POST", f"{API}/materials", {"title": "Гость", "content": "Текст."}),
        ("DELETE", f"{API}/materials/{doc['id']}", None),
        ("GET", f"{API}/folders", None),
        ("GET", f"{API}/conversations", None),
        ("POST", f"{API}/conversations", {"question": "Когда дежурит охрана?"}),
        ("POST", f"{API}/faq/ask", {"question": "Когда дежурит охрана?"}),
        ("GET", f"{API}/suggestions", None),
        ("GET", f"{API}/usage", None),
    ]
    for method, url, body in checks:
        r = await guest.request(method, url, json=body)
        assert r.status_code in UNAUTHENTICATED, (method, url, r.status_code)
        assert "Ель" not in r.text
    r = await admin.get(f"{API}/materials/{doc['id']}")
    assert r.status_code == 200


async def test_other_company_admin_cannot_touch_documents(kronto: Kronto) -> None:
    """ТЗ §2, §7: администратор управляет документами только своей компании; чужой
    документ не прочитать, не переименовать, не удалить."""
    admin_a = await make_admin(kronto, "alfa")
    admin_b = await make_admin(kronto, "beta")
    content = "Дуб. Пропуск сотрудника выдаёт охранник Василий."
    question = "Кто выдаёт пропуск сотрудника?"
    assert_shares_words(question, content)
    doc = await add_doc(admin_a, "Дуб: пропуска", content)
    await kronto.run_background()

    url = f"{API}/materials/{doc['id']}"
    r = await admin_b.get(url)
    assert r.status_code in DENIED and "Дуб" not in r.text, r.text
    r = await admin_b.patch(url, json={"title": "Захвачено"})
    assert r.status_code in DENIED, r.text
    r = await admin_b.post(f"{url}/ingest")
    assert r.status_code in DENIED, r.text
    r = await admin_b.delete(url)
    assert r.status_code in DENIED, r.text
    r = await admin_b.get(f"{API}/materials")
    assert r.status_code == 200 and doc["id"] not in r.text

    r = await admin_a.get(url)
    assert r.status_code == 200 and r.json()["title"] == "Дуб: пропуска"
    reply = await ask(admin_a, question)
    assert reply.origin == "documents" and doc["id"] in reply.material_ids


async def test_company_documents_never_appear_in_other_company_answers(
    kronto: Kronto,
) -> None:
    """ТЗ §2, §6: данные компаний разделены — документы компании А не попадают в
    ответы, источники и поиск компании Б."""
    admin_a = await make_admin(kronto, "alfa")
    admin_b = await make_admin(kronto, "beta")
    content = "Липа. Пропуск сотрудника выдаёт охранник Василий."
    question = "Кто выдаёт пропуск сотрудника?"
    assert_shares_words(question, content)
    doc = await add_doc(admin_a, "Липа: пропуска", content)
    await kronto.run_background()

    control = await ask(admin_a, question)
    assert control.origin == "documents" and doc["id"] in control.material_ids

    reply = await ask(admin_b, question)
    assert reply.origin != "documents", reply.answer
    assert_no_leak(reply.text, doc["id"], "Липа", "Василий")
    r = await faq_ask(admin_b, question)
    assert r.json()["origin"] != "documents"
    assert_no_leak(r.text, doc["id"], "Липа", "Василий")
    r = await admin_b.post(
        f"{API}/faq/search", json={"question": question, "limit": 50}
    )
    assert r.status_code == 200, r.text
    assert r.json()["matches"] == [] or all(
        m["material_id"] != doc["id"] for m in r.json()["matches"]
    )
    assert_no_leak(r.text, doc["id"], "Липа", "Василий")
    r = await admin_b.get(f"{API}/materials")
    assert r.status_code == 200 and doc["id"] not in r.text


async def test_person_in_two_companies_gets_answers_of_current_company_only(
    kronto: Kronto,
) -> None:
    """ТЗ §2: человек может состоять в нескольких компаниях и переключаться;
    документы и диалоги одной компании не видны, пока он работает в другой."""
    admin = await make_admin(kronto, "alfa")
    tenant_a = admin.tenant_id
    content = "Клён. Пропуск сотрудника выдаёт охранник Василий."
    question = "Кто выдаёт пропуск сотрудника?"
    assert_shares_words(question, content)
    doc = await add_doc(admin, "Клён: пропуска", content)
    await kronto.run_background()
    in_a = await ask(admin, question)
    assert in_a.origin == "documents" and doc["id"] in in_a.material_ids

    company_b = await kronto.create_company(
        code="beta", name="Компания Бета", admin_email=admin.email
    )
    assert company_b["account_created"] is False
    r = await admin.post(
        f"{API}/auth/switch-company", json={"tenant_id": company_b["id"]}
    )
    assert r.status_code == 200, r.text
    admin.token = r.json()["access_token"]
    me = await admin.me()
    assert me["company"]["tenant_id"] == company_b["id"]
    assert {c["tenant_id"] for c in me["companies"]} >= {tenant_a, company_b["id"]}

    in_b = await ask(admin, question)
    assert in_b.origin != "documents", in_b.answer
    assert_no_leak(in_b.text, doc["id"], "Клён", "Василий")
    r = await admin.get(f"{API}/materials")
    assert r.status_code == 200 and doc["id"] not in r.text
    # допущение: диалоги принадлежат компании (ТЗ §2: «его диалоги в этой компании»).
    assert in_a.conversation_id not in [
        c["id"] for c in await list_conversations(admin)
    ]
    r = await admin.get(f"{API}/conversations/{in_a.conversation_id}")
    assert r.status_code in DENIED, r.text

    r = await admin.post(f"{API}/auth/switch-company", json={"tenant_id": tenant_a})
    assert r.status_code == 200, r.text
    admin.token = r.json()["access_token"]
    back = await ask(admin, question)
    assert back.origin == "documents" and doc["id"] in back.material_ids


async def test_restricted_folder_answers_only_its_department_and_admins(
    kronto: Kronto,
) -> None:
    """ТЗ §5, §7: папка с доступом по отделам — документ папки отвечает только
    сотрудникам этого отдела и администраторам; сотрудник другого отдела не видит
    ни ответа, ни источника, ни фрагмента (ни в чате, ни в /faq/ask)."""
    setup = await setup_restricted(kronto)
    assert setup.insider is not None and setup.outsider is not None

    await assert_secret_visible_to(setup.insider, setup.secret_id)
    insider_reply = await ask(setup.insider, SECRET_QUESTION)
    source = next(
        s for s in insider_reply.sources if s["material_id"] == setup.secret_id
    )
    assert source["content"] and "7931" in source["content"]
    r = await faq_ask(setup.insider, SECRET_QUESTION)
    assert r.json()["origin"] == "documents"
    assert setup.secret_id in [s["material_id"] for s in r.json()["sources"]]

    # Администратор видит закрытые папки (схема: «только отделы и администраторы»).
    await assert_secret_visible_to(setup.admin, setup.secret_id)

    await assert_secret_hidden_from(setup.outsider, setup.secret_id)
    reply = await ask(setup.outsider, SECRET_QUESTION)
    assert_no_leak(reply.text, "Гранат")

    # Поиск у постороннего работает — общий документ он находит.
    control = await ask(setup.outsider, COMMON_QUESTION)
    assert control.origin == "documents" and setup.common_id in control.material_ids
    assert_no_leak(control.text, setup.secret_id, *SECRET_MARKERS)


async def test_restricted_document_hidden_in_lists_for_other_department(
    kronto: Kronto,
) -> None:
    """ТЗ §5: «где ищет ассистент» у сотрудника — только доступные ему папки;
    закрытый документ не открыть и не увидеть в списках."""
    setup = await setup_restricted(kronto)
    assert setup.insider is not None and setup.outsider is not None

    r = await setup.outsider.get(f"{API}/materials/{setup.secret_id}")
    assert r.status_code in DENIED, r.text
    assert_no_leak(r.text, *SECRET_MARKERS)
    r = await setup.outsider.get(f"{API}/materials")
    # допущение: список документов сотруднику либо закрыт, либо без чужих папок.
    if r.status_code == 200:
        assert setup.secret_id not in r.text
        assert_no_leak(r.text, *SECRET_MARKERS)
    else:
        assert r.status_code in DENIED, r.text

    r = await setup.outsider.get(f"{API}/sources/mine")
    assert r.status_code == 200, r.text
    outsider_files = r.json()["files"]
    assert all(
        g["folder_id"] != setup.folder_id or g["documents"] == 0 for g in outsider_files
    ), outsider_files
    assert sum(g["documents"] for g in outsider_files if g["folder_id"] is None) >= 1

    r = await setup.insider.get(f"{API}/sources/mine")
    assert r.status_code == 200, r.text
    insider_groups = {g["folder_id"]: g for g in r.json()["files"]}
    assert setup.folder_id in insider_groups, r.text
    assert insider_groups[setup.folder_id]["documents"] >= 1
    assert insider_groups[setup.folder_id]["restricted"] is True


async def test_employee_without_department_does_not_see_restricted_folder(
    kronto: Kronto,
) -> None:
    """ТЗ §5, §7: документ, загруженный файлом сразу в закрытую папку, не виден
    сотруднику без отдела; администратор его находит."""
    admin = await make_admin(kronto, "alfa")
    employee = await join_employee(
        kronto, admin, "novichok@alfa-corp.ru", "Олег", "Новиков"
    )
    dept = await make_department(admin, "Бухгалтерия")
    folder = await make_folder(admin, FOLDER_NAME, [dept], restricted=True)
    r = await upload_doc(
        admin, SECRET_TITLE, "seif.txt", SECRET_CONTENT.encode(), folder_id=folder["id"]
    )
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["folder_id"] == folder["id"]
    await kronto.run_background()

    r = await admin.get(f"{API}/folders")
    assert r.status_code == 200, r.text
    listed = {f["id"]: f for f in r.json()}
    assert listed[folder["id"]]["documents"] == 1
    assert listed[folder["id"]]["restricted"] is True

    await assert_secret_hidden_from(employee, doc["id"])
    await assert_secret_visible_to(admin, doc["id"])


async def test_moving_document_into_restricted_folder_hides_it(kronto: Kronto) -> None:
    """ТЗ §5, §7: перенос документа в закрытую папку закрывает его для других
    отделов; возврат в общие документы — снова открывает."""
    setup = await setup_restricted(kronto, insider=False)
    outsider = setup.outsider
    assert outsider is not None

    reply = await ask(outsider, COMMON_QUESTION)
    assert reply.origin == "documents" and setup.common_id in reply.material_ids

    r = await setup.admin.patch(
        f"{API}/materials/{setup.common_id}", json={"folder_id": setup.folder_id}
    )
    assert r.status_code == 200, r.text
    assert r.json()["folder_id"] == setup.folder_id
    await kronto.run_background()

    reply = await ask(outsider, COMMON_QUESTION)
    assert reply.origin != "documents", reply.answer
    assert_no_leak(reply.text, setup.common_id, COMMON_MARKER)
    r = await faq_ask(outsider, COMMON_QUESTION)
    assert_no_leak(r.text, setup.common_id, COMMON_MARKER)

    r = await setup.admin.patch(
        f"{API}/materials/{setup.common_id}", json={"folder_id": None}
    )
    assert r.status_code == 200, r.text
    assert r.json()["folder_id"] is None
    await kronto.run_background()
    reply = await ask(outsider, COMMON_QUESTION)
    assert reply.origin == "documents" and setup.common_id in reply.material_ids


async def test_leaving_department_revokes_folder_access(kronto: Kronto) -> None:
    """ТЗ §7: доступ к папкам привязан к отделам — администратор убрал сотрудника
    из отдела, и документы закрытой папки ему больше не отвечают."""
    setup = await setup_restricted(kronto, outsider=False)
    insider = setup.insider
    assert insider is not None
    await assert_secret_visible_to(insider, setup.secret_id)

    await set_department(setup.admin, insider.member_id, None)
    await kronto.run_background()
    await assert_secret_hidden_from(insider, setup.secret_id)


async def test_adding_department_to_folder_grants_access(kronto: Kronto) -> None:
    """ТЗ §5, §7: администратор открыл папку ещё одному отделу — его сотрудники
    начинают получать ответы по документам папки."""
    setup = await setup_restricted(kronto, insider=False)
    outsider = setup.outsider
    assert outsider is not None
    await assert_secret_hidden_from(outsider, setup.secret_id)

    r = await setup.admin.patch(
        f"{API}/folders/{setup.folder_id}",
        json={"department_ids": [setup.dept_in, setup.dept_out]},
    )
    assert r.status_code == 200, r.text
    assert {d["id"] for d in r.json()["departments"]} == {setup.dept_in, setup.dept_out}
    await kronto.run_background()
    await assert_secret_visible_to(outsider, setup.secret_id)


async def test_folder_rules_and_validation(kronto: Kronto) -> None:
    """ТЗ §5, §7: папки заводит администратор; папку с документами удалить нельзя
    (иначе закрытое стало бы видно всем), пустую — можно; пустое и длинное имя —
    отказ; отдел чужой компании к папке не привязать."""
    admin = await make_admin(kronto, "alfa")
    admin_b = await make_admin(kronto, "beta")
    dept = await make_department(admin, "Бухгалтерия")
    foreign_dept = await make_department(admin_b, "Чужой отдел")

    for bad in ({"name": "   "}, {"name": ""}, {"name": "П" * 101}):
        r = await admin.post(f"{API}/folders", json=bad)
        assert r.status_code == 422, (bad, r.status_code)

    r = await admin.post(
        f"{API}/folders",
        json={
            "name": "Смешанная",
            "restricted": True,
            "department_ids": [foreign_dept],
        },
    )
    # допущение: чужой отдел — отказ 4xx либо молча не привязывается.
    if r.status_code == 201:
        assert foreign_dept not in [d["id"] for d in r.json()["departments"]], r.text
    else:
        assert 400 <= r.status_code < 500, r.text

    folder = await make_folder(admin, FOLDER_NAME, [dept])
    doc = await add_doc(admin, SECRET_TITLE, SECRET_CONTENT, folder_id=folder["id"])
    await kronto.run_background()

    r = await admin.delete(f"{API}/folders/{folder['id']}")
    assert 400 <= r.status_code < 500, r.text  # допущение: код отказа не задан
    r = await admin.get(f"{API}/folders")
    assert folder["id"] in [f["id"] for f in r.json()]
    r = await admin.get(f"{API}/materials/{doc['id']}")
    assert r.status_code == 200 and r.json()["folder_id"] == folder["id"]

    r = await admin.patch(f"{API}/materials/{doc['id']}", json={"folder_id": None})
    assert r.status_code == 200, r.text
    r = await admin.delete(f"{API}/folders/{folder['id']}")
    assert r.status_code == 204, r.text
    r = await admin.get(f"{API}/folders")
    assert folder["id"] not in [f["id"] for f in r.json()]

    r = await admin_b.patch(f"{API}/folders/{folder['id']}", json={"name": "Чужая"})
    assert r.status_code in DENIED, r.text


async def test_deleted_document_disappears_from_answers(kronto: Kronto) -> None:
    """ТЗ §5, §7: удалённый документ больше не попадает в ответы (ни в чате, ни в
    /faq/ask), а в старом диалоге его фрагмент не показывается."""
    admin = await make_admin(kronto, "alfa")
    content = "Осина. Спецодежду выдают на складе по четвергам."
    question = "Где выдают спецодежду?"
    assert_shares_words(question, content)
    doc = await add_doc(admin, "Осина: спецодежда", content)
    await kronto.run_background()

    old = await ask(admin, question)
    assert old.origin == "documents" and doc["id"] in old.material_ids

    r = await admin.delete(f"{API}/materials/{doc['id']}")
    assert r.status_code == 204, r.text
    r = await admin.get(f"{API}/materials/{doc['id']}")
    assert r.status_code == 404, r.text
    r = await admin.get(f"{API}/materials")
    assert doc["id"] not in r.text
    await kronto.run_background()

    reply = await ask(admin, question)
    assert reply.origin != "documents", reply.answer
    assert_no_leak(reply.text, doc["id"], "Осина", "четвергам")
    r = await faq_ask(admin, question)
    assert r.json()["origin"] != "documents"
    assert_no_leak(r.text, doc["id"], "Осина", "четвергам")

    # схема: content=null — документ удалён.
    conversation = await get_conversation(admin, old.conversation_id)
    sources = [s for m in conversation["messages"] for s in m["sources"]]
    for source in sources:
        if source["material_id"] == doc["id"] or source["kind"] == "document":
            assert source["content"] is None, source


async def test_renamed_document_is_reindexed_with_new_title(kronto: Kronto) -> None:
    """ТЗ §7 (документы): переименование — документ переиндексируется, источник в
    ответе подписан новым названием; пустое название — отказ."""
    admin = await make_admin(kronto, "alfa")
    content = "Ива. Ноутбуки выдаёт служба поддержки в первый день."
    question = "Кто выдаёт ноутбуки?"
    assert_shares_words(question, content)
    doc = await add_doc(admin, "Ива: техника", content)
    await kronto.run_background()

    r = await admin.patch(f"{API}/materials/{doc['id']}", json={"title": ""})
    assert r.status_code == 422, r.text
    r = await admin.patch(
        f"{API}/materials/{doc['id']}", json={"title": "Ива: выдача ноутбуков"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Ива: выдача ноутбуков"
    await kronto.run_background()
    r = await admin.get(f"{API}/materials/{doc['id']}")
    assert r.json()["status"] == "ready"

    reply = await ask(admin, question)
    source = next(s for s in reply.sources if s["material_id"] == doc["id"])
    assert source["title"] == "Ива: выдача ноутбуков"

    r = await admin.post(f"{API}/materials/{doc['id']}/ingest")
    assert r.status_code == 202, r.text
    assert r.json()["material_id"] == doc["id"]
    await kronto.run_background()
    r = await admin.get(f"{API}/materials/{doc['id']}")
    assert r.json()["status"] == "ready"


# ===========================================================================
# Ответы: режим «ответа нет», поток, сбой модели, кредиты, глоссарий
# ===========================================================================


async def test_general_mode_answer_is_marked_as_not_from_documents(
    kronto: Kronto,
) -> None:
    """ТЗ §7 (режим «ответа нет»), §6: по умолчанию, если в документах ничего нет,
    — общий ответ с явной пометкой «не из документов», без источников."""
    admin = await make_admin(kronto, "alfa")
    content = "Бук. Охрана дежурит у главного входа."
    question = "Сколько стоит абонемент бассейна?"
    assert_no_shared_words(question, content, "Бук: охрана")
    await add_doc(admin, "Бук: охрана", content)
    await kronto.run_background()

    r = await admin.get(f"{API}/company")
    assert r.status_code == 200 and r.json()["not_found_mode"] == "general", r.text

    reply = await ask(admin, question)
    assert reply.ok, reply.final
    assert reply.origin == "general_knowledge"
    assert reply.content.startswith(NOT_FOUND_PREFIX), reply.content
    assert STUB_GENERAL in reply.content
    assert reply.sources == []
    assert "Бук" not in reply.content

    r = await faq_ask(admin, question)
    body = r.json()
    assert body["origin"] == "general_knowledge"
    assert body["content"].startswith(NOT_FOUND_PREFIX)
    assert body["sources"] == []


async def test_strict_mode_refuses_without_general_answer(kronto: Kronto) -> None:
    """ТЗ §7: в строгом режиме, если в документах ответа нет, — честный отказ без
    ответа из общих знаний; по документам компания отвечает как обычно."""
    admin = await make_admin(kronto, "alfa", not_found_mode="strict")
    content = "Граб. Охрана дежурит у главного входа."
    question = "Сколько стоит абонемент бассейна?"
    assert_no_shared_words(question, content, "Граб: охрана")
    doc = await add_doc(admin, "Граб: охрана", content)
    await kronto.run_background()

    r = await admin.get(f"{API}/company")
    assert r.json()["not_found_mode"] == "strict", r.text

    reply = await ask(admin, question)
    assert reply.origin == "none", reply.answer
    assert reply.content.startswith(NOT_FOUND_PREFIX), reply.content
    assert STUB_GENERAL not in reply.content
    assert reply.sources == []

    r = await faq_ask(admin, question)
    assert r.json()["origin"] == "none"
    assert STUB_GENERAL not in r.json()["content"]
    assert r.json()["sources"] == []

    assert_shares_words("Где дежурит охрана?", content)
    found = await ask(admin, "Где дежурит охрана?")
    assert found.origin == "documents" and doc["id"] in found.material_ids


async def test_admin_switches_not_found_mode_in_settings(kronto: Kronto) -> None:
    """ТЗ §7: режим «ответа нет» меняет администратор в настройках компании;
    сотрудник настройки не меняет."""
    admin = await make_admin(kronto, "alfa")
    employee = await join_employee(kronto, admin, "ivan@alfa-corp.ru")
    question = "Сколько стоит абонемент бассейна?"

    r = await employee.patch(f"{API}/company", json={"not_found_mode": "strict"})
    assert r.status_code in DENIED, r.text
    reply = await ask(employee, question)
    assert reply.origin == "general_knowledge"

    r = await admin.patch(f"{API}/company", json={"not_found_mode": "strict"})
    assert r.status_code == 200, r.text
    assert r.json()["not_found_mode"] == "strict"
    reply = await ask(employee, question)
    assert reply.origin == "none", reply.answer
    assert STUB_GENERAL not in reply.content

    r = await admin.patch(f"{API}/company", json={"not_found_mode": "nonsense"})
    assert r.status_code == 422, r.text
    r = await admin.patch(f"{API}/company", json={"not_found_mode": "general"})
    assert r.status_code == 200 and r.json()["not_found_mode"] == "general"
    reply = await ask(employee, question)
    assert reply.origin == "general_knowledge"


async def test_answer_streams_word_by_word_and_is_saved(kronto: Kronto) -> None:
    """ТЗ §6: ответ печатается по мере генерации (поток событий: начало, кусочки
    текста, итог) и сохраняется в диалоге на сервере вместе с источниками."""
    admin = await make_admin(kronto, "alfa")
    content = "Тис. Больничный оформляют через отдел кадров в день выхода."
    question = "Как оформляют больничный?"
    assert_shares_words(question, content)
    doc = await add_doc(admin, "Тис: больничный", content)
    await kronto.run_background()

    reply = await ask(admin, question)
    types = reply.types
    assert types[0] == "start" and types[-1] == "done", types
    assert "error" not in types
    assert types.count("delta") >= 2, types  # «по словам», а не одним куском
    streamed = "".join(e["text"] for e in reply.events if e.get("type") == "delta")
    assert "Режим" in streamed and "Тис" in streamed, streamed

    start = reply.start
    assert start["question"]["role"] == "user"
    assert start["question"]["content"] == question
    assert start["answer"]["role"] == "assistant"
    assert start["answer"]["parent_id"] == start["question"]["id"]
    # допущение: в начале потока ответ ещё пишется.
    assert start["answer"]["status"] == "generating"

    final = reply.final["answer"]
    assert final["status"] == "complete"
    assert final["origin"] == "documents"
    assert doc["id"] in [s["material_id"] for s in final["sources"]]

    conversation = await get_conversation(admin, reply.conversation_id)
    saved = conversation["messages"][-1]
    assert saved["id"] == reply.answer_id
    assert saved["status"] == "complete"
    assert saved["content"] == final["content"]
    assert doc["id"] in [s["material_id"] for s in saved["sources"]]
    assert conversation["current_message_id"] == reply.answer_id


async def test_employee_gets_no_admin_diagnostics(kronto: Kronto) -> None:
    """ТЗ §2 (роли), §6: сотрудник получает ответ без служебной отладки
    (схема: diagnostics — только для администратора)."""
    admin = await make_admin(kronto, "alfa")
    employee = await join_employee(kronto, admin, "ivan@alfa-corp.ru")
    content = "Самшит. Канцелярию выдаёт офис-менеджер."
    question = "Кто выдаёт канцелярию?"
    assert_shares_words(question, content)
    await add_doc(admin, "Самшит: канцелярия", content)
    await kronto.run_background()

    reply = await ask(employee, question)
    assert reply.ok and reply.origin == "documents"
    assert reply.final.get("diagnostics") is None, reply.final
    r = await faq_ask(employee, question)
    assert r.json().get("diagnostics") is None, r.text
    r = await employee.post(f"{API}/faq/search", json={"question": question})
    assert r.status_code in DENIED, r.text


async def test_model_outage_fails_answer_and_regenerate_recovers(
    kronto: Kronto,
) -> None:
    """ТЗ §6: сбой поставщика модели — ответ не выдумывается, а помечается
    неудачным (с причиной); кредиты за него не списываются; после восстановления
    «Ответить заново» даёт нормальный ответ."""
    admin = await make_admin(kronto, "alfa")
    content = "Пихта. Отпуск согласует руководитель отдела."
    question = "Кто согласует отпуск?"
    assert_shares_words(question, content)
    doc = await add_doc(admin, "Пихта: отпуск", content)
    await kronto.run_background()
    used_before = (await get_usage(admin))["used"]

    kronto.model.unavailable = True
    try:
        r = await admin.post(f"{API}/conversations", json={"question": question})
        assert r.status_code == 200, r.text
        events = sse_events(r.text)
        assert events[0]["type"] == "start", events
        assert events[-1]["type"] == "error", events[-1]
        assert "done" not in [e["type"] for e in events]
        conversation_id = events[0]["conversation"]["id"]
        question_id = events[0]["question"]["id"]
        conversation = await get_conversation(admin, conversation_id)
        failed = conversation["messages"][-1]
        assert failed["role"] == "assistant"
        assert failed["status"] == "failed", failed
        # допущение: код из перечня схемы для недоступного поставщика модели.
        assert failed["error_code"] == "llm_unavailable", failed
        # допущение: неудачный ответ не тратит кредиты пула.
        assert (await get_usage(admin))["used"] == used_before
    finally:
        kronto.model.unavailable = False

    retry = await regenerate(admin, conversation_id, question_id)
    assert retry.ok, retry.final
    assert retry.origin == "documents" and doc["id"] in retry.material_ids
    conversation = await get_conversation(admin, conversation_id)
    assert conversation["messages"][-1]["status"] == "complete"


async def test_usage_pool_is_per_company_and_admin_only(kronto: Kronto) -> None:
    """ТЗ §7 (тариф: места, расход): пул кредитов — общий на компанию, считается
    по местам; вопросы тратят пул своей компании; расход видит только админ."""
    admin_a = await make_admin(kronto, "alfa", seats=3)
    admin_b = await make_admin(kronto, "beta", seats=3)
    employee = await join_employee(kronto, admin_a, "ivan@alfa-corp.ru")

    usage = await get_usage(admin_a)
    assert usage["seats"] == 3
    # допущение: пул = места × кредиты на место (поля схемы).
    assert usage["pool"] == usage["seats"] * usage["credits_per_seat"]
    assert usage["used"] == 0 and usage["remaining"] == usage["pool"]
    assert usage["exhausted"] is False and usage["warning"] is False

    content = "Ель. Обед длится один час."
    question = "Сколько длится обед?"
    assert_shares_words(question, content)
    await add_doc(admin_a, "Ель: обед", content)
    await kronto.run_background()
    reply = await ask(employee, question)
    assert reply.ok

    after = await get_usage(admin_a)
    assert after["used"] > 0
    assert after["remaining"] == after["pool"] - after["used"]
    assert (await get_usage(admin_b))["used"] == 0

    r = await employee.get(f"{API}/usage")
    assert r.status_code in DENIED, r.text


async def test_answers_stop_when_credit_pool_is_exhausted(kronto: Kronto) -> None:
    """ТЗ §7, §8: пул кредитов кончился (100 %) — ответы останавливаются (модель
    больше не вызывается, ответ помечен «кредиты исчерпаны»), админу приходит
    уведомление."""
    # Разбор 06.10: пул на одно место — 420 кредитов, тест пропускался;
    # обвязка теперь задаёт кредиты на место, как настройка сервера.
    kronto.set_credits_per_seat(6)
    admin = await make_admin(kronto, "kred", seats=1)
    content = "Вяз. Обед длится один час."
    question = "Сколько длится обед?"
    assert_shares_words(question, content)
    await add_doc(admin, "Вяз: обед", content)
    await kronto.run_background()

    u0 = await get_usage(admin)
    first = await ask(admin, question)
    assert first.ok
    u1 = await get_usage(admin)
    cost = u1["used"] - u0["used"]
    assert cost > 0
    needed = math.ceil(max(u1["remaining"], 0) / cost)
    if needed > 40:
        pytest.skip(
            f"пул на одно место слишком велик для теста: нужно {needed} вопросов"
        )

    for _ in range(needed + 3):
        r = await admin.post(f"{API}/conversations", json={"question": question})
        if r.status_code == 429:
            pytest.skip("сработал лимит частоты вопросов раньше, чем кончился пул")
        if r.status_code != 200:
            break
        if sse_events(r.text)[-1]["type"] == "error":
            break

    usage = await get_usage(admin)
    assert usage["exhausted"] is True, usage
    assert usage["remaining"] == max(0, usage["pool"] - usage["used"]), usage

    calls_before = kronto.model.calls
    r = await admin.post(f"{API}/conversations", json={"question": question})
    if r.status_code == 200:
        events = sse_events(r.text)
        assert events[-1]["type"] == "error", events[-1]
        error = events[-1]
        answer = error.get("answer") or events[0]["answer"]
        assert "credits_exhausted" in (error.get("code"), answer.get("error_code")), (
            error
        )
        conversation = await get_conversation(admin, events[0]["conversation"]["id"])
        last = conversation["messages"][-1]
        assert last["status"] == "failed" and last["error_code"] == "credits_exhausted"
    else:
        # допущение: отказ до начала потока тоже допустим, но только 4xx.
        assert 400 <= r.status_code < 500, r.text
    assert kronto.model.calls == calls_before, "после 100 % модель вызываться не должна"

    await kronto.run_background()
    r = await admin.get(f"{API}/notifications")
    assert r.status_code == 200, r.text
    assert "credits_exhausted" in [n["kind"] for n in r.json()["items"]], r.text


async def test_glossary_expands_abbreviations_in_questions(kronto: Kronto) -> None:
    """ТЗ §7 (глоссарий): сокращение из словаря компании расшифровывается в вопросе
    — документ находится по сокращению; удалили термин — снова не находится."""
    admin = await make_admin(kronto, "alfa")
    title = "Тюльпан: полис"
    content = (
        "Тюльпан: полис, добровольное медицинское страхование, "
        "оформляет Мария Ивановна."
    )
    question = "Как получить ДМС?"
    assert_no_shared_words(question, title, content)
    assert_shares_words("добровольное медицинское страхование", content)
    doc = await add_doc(admin, title, content)
    await kronto.run_background()

    before = await ask(admin, question)
    assert before.origin != "documents"

    r = await admin.post(
        f"{API}/glossary",
        json={"term": "ДМС", "expansion": "добровольное медицинское страхование"},
    )
    assert r.status_code == 201, r.text
    term = r.json()
    r = await admin.get(f"{API}/glossary")
    assert term["id"] in [t["id"] for t in r.json()]

    after = await ask(admin, question)
    assert after.origin == "documents" and doc["id"] in after.material_ids
    assert "Тюльпан" in after.content

    r = await admin.delete(f"{API}/glossary/{term['id']}")
    assert r.status_code == 204, r.text
    again = await ask(admin, question)
    assert again.origin != "documents"


async def test_glossary_is_per_company_and_admin_only(kronto: Kronto) -> None:
    """ТЗ §2, §7: глоссарий — свой у каждой компании и ведёт его администратор;
    чужой словарь на ответы не влияет; пустой и слишком длинный ввод — отказ."""
    admin_a = await make_admin(kronto, "alfa")
    admin_b = await make_admin(kronto, "beta")
    employee = await join_employee(kronto, admin_a, "ivan@alfa-corp.ru")
    content = (
        "Нарцисс: полис, добровольное медицинское страхование, "
        "оформляет Мария Ивановна."
    )
    question = "Как получить ДМС?"
    assert_no_shared_words(question, content, "Нарцисс: полис")
    await add_doc(admin_a, "Нарцисс: полис", content)
    await kronto.run_background()

    r = await admin_b.post(
        f"{API}/glossary",
        json={"term": "ДМС", "expansion": "добровольное медицинское страхование"},
    )
    assert r.status_code == 201, r.text
    term_b = r.json()
    reply = await ask(employee, question)
    assert reply.origin != "documents"
    r = await admin_a.get(f"{API}/glossary")
    assert term_b["id"] not in r.text
    r = await admin_a.delete(f"{API}/glossary/{term_b['id']}")
    assert r.status_code in DENIED, r.text

    r = await employee.post(
        f"{API}/glossary",
        json={"term": "ДМС", "expansion": "добровольное медицинское страхование"},
    )
    assert r.status_code in DENIED, r.text
    for bad in (
        {"term": "Д", "expansion": "добровольное"},
        {"term": "ДМС", "expansion": "д"},
        {"term": "Т" * 65, "expansion": "длинный термин"},
        {"term": "ДМС", "expansion": "р" * 257},
    ):
        r = await admin_a.post(f"{API}/glossary", json=bad)
        assert r.status_code == 422, (bad, r.status_code)


async def test_question_length_limit_is_4000(kronto: Kronto) -> None:
    """ТЗ §6: вопрос — до 4 000 символов; длиннее и пустой — отказ."""
    admin = await make_admin(kronto, "alfa")
    longest = ("отпуск " * 700)[:4000]
    assert len(longest) == 4000
    too_long = ("отпуск " * 700)[:4001]

    reply = await ask(admin, longest)
    assert reply.ok, reply.final
    r = await admin.post(f"{API}/conversations", json={"question": too_long})
    assert r.status_code == 422, r.status_code
    r = await admin.post(f"{API}/conversations", json={"question": ""})
    assert r.status_code == 422, r.status_code
    r = await admin.post(
        f"{API}/conversations/{reply.conversation_id}/messages",
        json={"question": too_long, "parent_id": reply.answer_id},
    )
    assert r.status_code == 422, r.status_code
    r = await admin.post(f"{API}/faq/ask", json={"question": too_long})
    assert r.status_code == 422, r.status_code

    items = await list_conversations(admin)
    assert [c["id"] for c in items] == [reply.conversation_id]
    conversation = await get_conversation(admin, reply.conversation_id)
    assert len(conversation["messages"]) == 2


# ===========================================================================
# Чат: диалоги на сервере (ТЗ §6)
# ===========================================================================


async def test_first_question_creates_named_conversation_on_server(
    kronto: Kronto,
) -> None:
    """ТЗ §6: диалоги хранятся на сервере и видны в списке; название — по первому
    вопросу; вопрос и ответ связаны в ветку."""
    admin = await make_admin(kronto, "alfa")
    employee = await join_employee(kronto, admin, "ivan@alfa-corp.ru")
    question = "Сколько дней отпуска положено новичку?"
    reply = await ask(employee, question)
    assert reply.ok

    summary = reply.start["conversation"]
    assert summary["title"]
    # допущение: название — из слов первого вопроса.
    assert words(summary["title"]) & words(question), summary["title"]
    assert summary["pinned"] is False and summary["shared"] is False

    items = await list_conversations(employee)
    assert [c["id"] for c in items] == [reply.conversation_id]
    assert items[0]["title"] == summary["title"]

    # Перезагрузка страницы: сеанс восстанавливается по refresh-cookie, диалог —
    # с сервера, а не из вкладки.
    r = await employee.post(f"{API}/auth/refresh")
    assert r.status_code == 200, r.text
    employee.token = r.json()["access_token"]
    await employee.me()
    if employee.tenant_id != admin.tenant_id:
        r = await employee.post(
            f"{API}/auth/switch-company", json={"tenant_id": admin.tenant_id}
        )
        assert r.status_code == 200, r.text
        employee.token = r.json()["access_token"]
    conversation = await get_conversation(employee, reply.conversation_id)
    messages = conversation["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == question
    assert messages[0]["parent_id"] is None
    assert messages[1]["parent_id"] == messages[0]["id"]
    assert conversation["title"] == summary["title"]


async def test_follow_up_question_continues_conversation(kronto: Kronto) -> None:
    """ТЗ §6: следующий вопрос продолжает тот же диалог; название остаётся по
    первому вопросу."""
    admin = await make_admin(kronto, "alfa")
    first = await ask(admin, "Когда выплачивают аванс?")
    title = first.start["conversation"]["title"]
    second = await ask(
        admin,
        "А зарплату когда?",
        conversation_id=first.conversation_id,
        parent_id=first.answer_id,
    )
    assert second.ok
    assert second.conversation_id == first.conversation_id

    conversation = await get_conversation(admin, first.conversation_id)
    messages = conversation["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[2]["content"] == "А зарплату когда?"
    assert messages[2]["parent_id"] == first.answer_id
    assert messages[3]["id"] == second.answer_id
    assert conversation["title"] == title
    assert len(await list_conversations(admin)) == 1

    r = await admin.post(
        f"{API}/conversations/{uuid.uuid4()}/messages",
        json={"question": "Вопрос в никуда", "parent_id": None},
    )
    assert r.status_code == 404, r.text


async def test_pin_and_rename_conversations(kronto: Kronto) -> None:
    """ТЗ §6: диалог можно закрепить (закреплённые — сверху списка), открепить и
    переименовать; пустое и слишком длинное название — отказ."""
    admin = await make_admin(kronto, "alfa")
    older = await ask(admin, "Где взять бланк командировки?")
    newer = await ask(admin, "Кто подписывает заявление на отгул?")
    ids = [c["id"] for c in await list_conversations(admin)]
    assert ids == [newer.conversation_id, older.conversation_id]

    r = await admin.patch(
        f"{API}/conversations/{older.conversation_id}", json={"pinned": True}
    )
    assert r.status_code == 200 and r.json()["pinned"] is True, r.text
    await ask(
        admin,
        "А на сколько дней?",
        conversation_id=newer.conversation_id,
        parent_id=newer.answer_id,
    )
    items = await list_conversations(admin)
    assert items[0]["id"] == older.conversation_id and items[0]["pinned"] is True

    r = await admin.patch(
        f"{API}/conversations/{older.conversation_id}", json={"pinned": False}
    )
    assert r.status_code == 200 and r.json()["pinned"] is False
    await ask(
        admin,
        "А кто его согласует?",
        conversation_id=newer.conversation_id,
        parent_id=newer.answer_id,
    )
    items = await list_conversations(admin)
    assert items[0]["id"] == newer.conversation_id  # откреплённый больше не сверху
    assert all(c["pinned"] is False for c in items)

    r = await admin.patch(
        f"{API}/conversations/{newer.conversation_id}", json={"title": "Отгулы"}
    )
    assert r.status_code == 200 and r.json()["title"] == "Отгулы"
    assert (await get_conversation(admin, newer.conversation_id))["title"] == "Отгулы"

    for bad in ("", "Н" * 121):
        r = await admin.patch(
            f"{API}/conversations/{newer.conversation_id}", json={"title": bad}
        )
        assert r.status_code == 422, (len(bad), r.status_code)
    assert (await get_conversation(admin, newer.conversation_id))["title"] == "Отгулы"


async def test_conversation_search_finds_only_own_conversations(kronto: Kronto) -> None:
    """ТЗ §6: поиск по диалогам — по своим диалогам; диалоги коллеги в поиск не
    попадают."""
    admin = await make_admin(kronto, "alfa")
    first = await join_employee(kronto, admin, "anna@alfa-corp.ru", "Анна", "Ключарёва")
    second = await join_employee(
        kronto, admin, "boris@alfa-corp.ru", "Борис", "Архипов"
    )
    archive = await ask(first, "Где хранятся ключи от архива?")
    salary = await ask(first, "Когда выплачивают аванс?")
    foreign = await ask(second, "Ключи от архива у кого?")

    found = [c["id"] for c in await list_conversations(first, q="архива")]
    assert found == [archive.conversation_id]
    assert foreign.conversation_id not in found
    found = [c["id"] for c in await list_conversations(first, q="аванс")]
    assert found == [salary.conversation_id]
    assert await list_conversations(first, q="ыщзщъхьжэ") == []
    found = [c["id"] for c in await list_conversations(second, q="архива")]
    assert found == [foreign.conversation_id]

    r = await first.get(f"{API}/conversations", params={"q": "а" * 201})
    assert r.status_code == 422, r.text


async def test_delete_conversation(kronto: Kronto) -> None:
    """ТЗ §6: свой диалог можно удалить; чужой — нельзя."""
    admin = await make_admin(kronto, "alfa")
    employee = await join_employee(kronto, admin, "ivan@alfa-corp.ru")
    reply = await ask(employee, "Где взять бланк командировки?")
    url = f"{API}/conversations/{reply.conversation_id}"

    r = await admin.delete(url)
    assert r.status_code in DENIED, r.text
    assert (await get_conversation(employee, reply.conversation_id))[
        "id"
    ] == reply.conversation_id

    r = await employee.delete(url)
    assert r.status_code == 204, r.text
    r = await employee.get(url)
    assert r.status_code == 404, r.text
    assert await list_conversations(employee) == []
    r = await employee.delete(url)
    assert r.status_code == 404, r.text


async def test_conversations_are_private_from_colleagues_and_admin(
    kronto: Kronto,
) -> None:
    """ТЗ §6: свои диалоги видит только сам человек; коллега и администратор не
    читают, не продолжают, не переименовывают и не публикуют чужой диалог."""
    admin = await make_admin(kronto, "alfa")
    owner = await join_employee(kronto, admin, "anna@alfa-corp.ru", "Анна", "Иванова")
    colleague = await join_employee(
        kronto, admin, "boris@alfa-corp.ru", "Борис", "Смирнов"
    )
    reply = await ask(owner, "Как оформить отпуск за свой счёт?")
    cid = reply.conversation_id
    base = f"{API}/conversations/{cid}"

    for intruder in (colleague, admin):
        r = await intruder.get(base)
        assert r.status_code in DENIED, r.text
        assert "свой счёт" not in r.text
        r = await intruder.patch(base, json={"title": "Чужое", "pinned": True})
        assert r.status_code in DENIED, r.text
        r = await intruder.post(
            f"{base}/messages",
            json={"question": "Подсмотреть", "parent_id": reply.answer_id},
        )
        assert r.status_code in DENIED, r.text
        r = await intruder.post(f"{base}/messages/{reply.question_id}/regenerate")
        assert r.status_code in DENIED, r.text
        r = await intruder.put(f"{base}/current", json={"message_id": reply.answer_id})
        assert r.status_code in DENIED, r.text
        r = await intruder.post(f"{base}/share")
        assert r.status_code in DENIED, r.text
        assert cid not in [c["id"] for c in await list_conversations(intruder)]
        assert await list_conversations(intruder, q="свой") == []

    conversation = await get_conversation(owner, cid)
    assert conversation["shared"] is False and conversation["pinned"] is False
    assert len(conversation["messages"]) == 2


async def test_regenerate_keeps_previous_answer_as_version(kronto: Kronto) -> None:
    """ТЗ §6: «Ответить заново» — новый ответ, прежний остаётся версией, между
    версиями можно переключаться."""
    admin = await make_admin(kronto, "alfa")
    content = "Кипарис. Пропуск на парковку оформляет охрана."
    question = "Кто оформляет пропуск на парковку?"
    assert_shares_words(question, content)
    await add_doc(admin, "Кипарис: парковка", content)
    await kronto.run_background()

    first = await ask(admin, question)
    again = await regenerate(admin, first.conversation_id, first.question_id)
    assert again.ok
    assert again.conversation_id == first.conversation_id
    assert again.start["question"]["id"] == first.question_id
    assert again.answer_id != first.answer_id
    assert set(again.start["answer"]["siblings"]) == {first.answer_id, again.answer_id}

    conversation = await get_conversation(admin, first.conversation_id)
    assert len(conversation["messages"]) == 2
    shown = conversation["messages"][-1]
    assert shown["id"] == again.answer_id
    assert shown["siblings"] == [first.answer_id, again.answer_id]

    r = await admin.put(
        f"{API}/conversations/{first.conversation_id}/current",
        json={"message_id": first.answer_id},
    )
    assert r.status_code == 200, r.text
    assert r.json()["messages"][-1]["id"] == first.answer_id
    conversation = await get_conversation(admin, first.conversation_id)
    assert conversation["messages"][-1]["id"] == first.answer_id
    assert conversation["current_message_id"] == first.answer_id

    other = await ask(admin, "Где взять бланк командировки?")
    r = await admin.put(
        f"{API}/conversations/{first.conversation_id}/current",
        json={"message_id": other.answer_id},
    )
    assert 400 <= r.status_code < 500, r.text  # допущение: чужое сообщение — 4xx


async def test_edit_question_creates_new_version(kronto: Kronto) -> None:
    """ТЗ §6: правка своего вопроса — новая версия вопроса с новым ответом,
    прежняя версия сохраняется и доступна."""
    admin = await make_admin(kronto, "alfa")
    first = await ask(admin, "Кто согласует отпуск?")
    edited = await ask(
        admin,
        "Кто согласует отгул?",
        conversation_id=first.conversation_id,
        parent_id=None,
    )
    assert edited.ok
    assert edited.conversation_id == first.conversation_id
    assert edited.question_id != first.question_id

    conversation = await get_conversation(admin, first.conversation_id)
    messages = conversation["messages"]
    assert len(messages) == 2
    assert messages[0]["id"] == edited.question_id
    assert messages[0]["content"] == "Кто согласует отгул?"
    assert set(messages[0]["siblings"]) == {first.question_id, edited.question_id}
    assert messages[1]["id"] == edited.answer_id

    r = await admin.put(
        f"{API}/conversations/{first.conversation_id}/current",
        json={"message_id": first.question_id},
    )
    assert r.status_code == 200, r.text
    messages = r.json()["messages"]
    assert messages[0]["id"] == first.question_id
    assert messages[0]["content"] == "Кто согласует отпуск?"
    assert first.answer_id in [m["id"] for m in messages]


async def test_stop_keeps_finished_answer_and_is_owner_only(kronto: Kronto) -> None:
    """ТЗ §6: кнопка «Остановить» — ответ сохраняется таким, каким успел быть;
    уже готовый ответ не меняется; остановить чужой ответ нельзя."""
    admin = await make_admin(kronto, "alfa")
    employee = await join_employee(kronto, admin, "ivan@alfa-corp.ru")
    reply = await ask(employee, "Где взять бланк командировки?")
    assert reply.ok
    url = f"{API}/conversations/{reply.conversation_id}/messages/{reply.answer_id}/stop"

    r = await admin.post(url)
    assert r.status_code in DENIED, r.text
    r = await employee.post(url)
    assert r.status_code == 204, r.text

    conversation = await get_conversation(employee, reply.conversation_id)
    answer = conversation["messages"][-1]
    assert answer["id"] == reply.answer_id
    assert answer["status"] == "complete"
    assert answer["content"] == reply.content

    r = await employee.post(
        f"{API}/conversations/{reply.conversation_id}/messages/{uuid.uuid4()}/stop"
    )
    assert 400 <= r.status_code < 500, r.text


async def test_feedback_thumbs_with_comment(kronto: Kronto) -> None:
    """ТЗ §6: 👍/👎 к ответу и комментарий «что не так»; оценку можно снять;
    оценивают только ответ и только свой; длинный комментарий — отказ."""
    admin = await make_admin(kronto, "alfa")
    employee = await join_employee(kronto, admin, "ivan@alfa-corp.ru")
    reply = await ask(employee, "Где взять бланк командировки?")
    base = f"{API}/conversations/{reply.conversation_id}/messages"
    url = f"{base}/{reply.answer_id}/feedback"

    r = await employee.put(
        url, json={"value": -1, "reason": "outdated", "comment": "Бланк уже другой"}
    )
    assert r.status_code == 204, r.text
    answer = (await get_conversation(employee, reply.conversation_id))["messages"][-1]
    assert answer["feedback"] == -1
    assert answer["feedback_reason"] == "outdated"
    assert answer["feedback_comment"] == "Бланк уже другой"

    r = await employee.put(url, json={"value": 1})
    assert r.status_code == 204, r.text
    answer = (await get_conversation(employee, reply.conversation_id))["messages"][-1]
    assert answer["feedback"] == 1

    r = await employee.put(url, json={"value": None})
    assert r.status_code == 204, r.text
    answer = (await get_conversation(employee, reply.conversation_id))["messages"][-1]
    assert answer["feedback"] is None

    # схема: reason — только к -1.
    r = await employee.put(url, json={"value": 1, "reason": "inaccurate"})
    assert 400 <= r.status_code < 500, r.text
    r = await employee.put(url, json={"value": -1, "comment": "к" * 1001})
    assert r.status_code == 422, r.text
    r = await employee.put(url, json={"value": 2})
    assert r.status_code == 422, r.text
    r = await employee.put(f"{base}/{reply.question_id}/feedback", json={"value": -1})
    assert 400 <= r.status_code < 500, r.text  # допущение: оценивается только ответ
    r = await admin.put(url, json={"value": -1, "comment": "Чужая оценка"})
    assert r.status_code in DENIED, r.text

    answer = (await get_conversation(employee, reply.conversation_id))["messages"][-1]
    assert answer["feedback"] is None and answer["feedback_comment"] != "Чужая оценка"


async def test_admin_sees_feedback_only_anonymised(kronto: Kronto) -> None:
    """ТЗ §6, §7: администратор видит оценки и комментарии к 👎 только обезличенно
    — без имени, почты и идентификатора сотрудника."""
    admin = await make_admin(kronto, "alfa")
    employee = await join_employee(
        kronto, admin, "evlampiya@alfa-corp.ru", "Евлампия", "Сидорчук"
    )
    reply = await ask(employee, "Какой телефон приёмной?")
    r = await employee.put(
        f"{API}/conversations/{reply.conversation_id}/messages/{reply.answer_id}/feedback",
        json={
            "value": -1,
            "reason": "inaccurate",
            "comment": "Телефон приёмной устарел",
        },
    )
    assert r.status_code == 204, r.text
    await kronto.run_background()

    r = await admin.get(f"{API}/analytics")
    assert r.status_code == 200, r.text
    analytics = r.json()
    assert analytics["dislikes"] >= 1
    assert "Телефон приёмной устарел" in [c["comment"] for c in analytics["comments"]]
    assert_no_leak(
        r.text, employee.email, "Евлампия", "Сидорчук", employee.member_id, "evlampiya"
    )

    r = await employee.get(f"{API}/analytics")
    assert r.status_code in DENIED, r.text


async def test_admin_sees_questions_only_anonymised(kronto: Kronto) -> None:
    """ТЗ §6, §7: администратор видит вопросы сотрудников только обезличенно —
    общие цифры и темы без имён; сами диалоги ему недоступны."""
    admin = await make_admin(kronto, "alfa")
    employee = await join_employee(
        kronto, admin, "evlampiya@alfa-corp.ru", "Евлампия", "Сидорчук"
    )
    question = "Положена ли компенсация за абонемент на скалодром?"
    reply = await ask(employee, question)
    assert reply.ok
    await kronto.run_background()

    r = await admin.get(f"{API}/analytics")
    assert r.status_code == 200, r.text
    assert r.json()["questions"] >= 1
    assert_no_leak(r.text, employee.email, "Евлампия", "Сидорчук", employee.member_id)

    r = await admin.get(f"{API}/conversations/{reply.conversation_id}")
    assert r.status_code in DENIED, r.text
    assert await list_conversations(admin, q="скалодром") == []

    r = await admin.get(f"{API}/audit")
    assert r.status_code == 200, r.text
    # допущение: журнал действий не хранит текст вопросов сотрудника (иначе
    # админ видел бы, кто что спросил).
    assert_no_leak(r.text, "скалодром")


# ===========================================================================
# Вложения к вопросу (ТЗ §6)
# ===========================================================================


ATTACHMENT_TEXT = (
    "Каштан. Неустойка поставщика составляет полпроцента в день просрочки."
)
ATTACHMENT_QUESTION = "Какая неустойка поставщика?"


async def test_attachment_answers_from_file_and_stays_out_of_company_base(
    kronto: Kronto,
) -> None:
    """ТЗ §6: вложение к вопросу («спроси по этому договору») — ответ по файлу,
    файл остаётся в диалоге и в базу компании не попадает."""
    assert_shares_words(ATTACHMENT_QUESTION, ATTACHMENT_TEXT)
    admin = await make_admin(kronto, "alfa")
    employee = await join_employee(kronto, admin, "ivan@alfa-corp.ru")
    data = ATTACHMENT_TEXT.encode()
    r = await upload_attachment(employee, "dogovor.txt", data)
    assert r.status_code == 201, r.text
    attachment = r.json()
    assert attachment["filename"] == "dogovor.txt"
    assert attachment["size"] == len(data)

    reply = await ask(employee, ATTACHMENT_QUESTION, attachment_ids=[attachment["id"]])
    assert reply.ok, reply.final
    from_file = [s for s in reply.sources if s["kind"] == "attachment"]
    assert from_file, reply.sources
    assert from_file[0]["attachment_id"] == attachment["id"]

    conversation = await get_conversation(employee, reply.conversation_id)
    question_message = conversation["messages"][0]
    assert attachment["id"] in [a["id"] for a in question_message["attachments"]]
    follow = await ask(
        employee,
        "Спасибо",
        conversation_id=reply.conversation_id,
        parent_id=reply.answer_id,
    )
    assert follow.ok
    conversation = await get_conversation(employee, reply.conversation_id)
    assert attachment["id"] in [
        a["id"] for a in conversation["messages"][0]["attachments"]
    ]

    await kronto.run_background()
    fresh = await ask(employee, ATTACHMENT_QUESTION)
    assert fresh.origin != "documents"
    assert all(s["kind"] != "attachment" for s in fresh.sources)
    assert_no_leak(fresh.text, "Каштан", attachment["id"])
    r = await admin.get(f"{API}/materials")
    assert r.status_code == 200 and r.json() == [], r.text
    r = await admin.post(f"{API}/faq/search", json={"question": ATTACHMENT_QUESTION})
    assert r.status_code == 200 and r.json()["matches"] == [], r.text
    admin_reply = await ask(admin, ATTACHMENT_QUESTION)
    assert_no_leak(admin_reply.text, "Каштан", attachment["id"])


async def test_attachment_belongs_to_its_uploader(kronto: Kronto) -> None:
    """ТЗ §6: вложение — часть своего вопроса: коллега не может задать вопрос с
    чужим вложением и удалить его; удалённое вложение к вопросу не прикрепить."""
    assert_shares_words(ATTACHMENT_QUESTION, ATTACHMENT_TEXT)
    admin = await make_admin(kronto, "alfa")
    owner = await join_employee(kronto, admin, "anna@alfa-corp.ru", "Анна", "Иванова")
    colleague = await join_employee(
        kronto, admin, "boris@alfa-corp.ru", "Борис", "Смирнов"
    )
    r = await upload_attachment(owner, "dogovor.txt", ATTACHMENT_TEXT.encode())
    assert r.status_code == 201, r.text
    attachment_id = r.json()["id"]

    r = await colleague.post(
        f"{API}/conversations",
        json={"question": ATTACHMENT_QUESTION, "attachment_ids": [attachment_id]},
    )
    # допущение: чужое вложение — отказ 4xx либо молча не используется.
    if r.status_code == 200:
        assert_no_leak(r.text, "Каштан", "полпроцента")
        for event in sse_events(r.text):
            answer = event.get("answer") or {}
            assert all(
                s.get("kind") != "attachment" for s in answer.get("sources") or []
            )
    else:
        assert 400 <= r.status_code < 500, r.text

    r = await colleague.delete(f"{API}/attachments/{attachment_id}")
    assert r.status_code in DENIED, r.text
    r = await owner.delete(f"{API}/attachments/{attachment_id}")
    assert r.status_code == 204, r.text

    r = await owner.post(
        f"{API}/conversations",
        json={"question": ATTACHMENT_QUESTION, "attachment_ids": [attachment_id]},
    )
    if r.status_code == 200:
        assert_no_leak(r.text, "Каштан", "полпроцента")
    else:
        assert 400 <= r.status_code < 500, r.text


async def test_attachment_format_size_and_count_limits(kronto: Kronto) -> None:
    """ТЗ §6: вложение — документ (схема: docx, doc, xlsx, pptx, pdf, txt, md до
    10 МБ, не больше 5 к вопросу); картинка, слишком большой файл и лишние
    вложения — отказ."""
    admin = await make_admin(kronto, "alfa")
    r = await upload_attachment(admin, "photo.png", PNG_1X1, "image/png")
    assert 400 <= r.status_code < 500, r.text
    r = await upload_attachment(
        admin, "tool.exe", EXE_BYTES, "application/octet-stream"
    )
    assert 400 <= r.status_code < 500, r.text

    big = ("слово " * (11 * 1024 * 1024 // 11 + 10)).encode()
    assert len(big) > 11 * 1024 * 1024
    r = await upload_attachment(admin, "big.txt", big)
    assert 400 <= r.status_code < 500, r.status_code  # допущение: 413 или 422

    r = await upload_attachment(
        admin, "small.md", "# Малый\n\nКороткий файл.".encode(), "text/markdown"
    )
    assert r.status_code == 201, r.text

    six = [str(uuid.uuid4()) for _ in range(6)]
    r = await admin.post(
        f"{API}/conversations",
        json={"question": "Что в файлах?", "attachment_ids": six},
    )
    assert r.status_code == 422, r.text
    r = await admin.post(
        f"{API}/conversations",
        json={"question": "Что в файле?", "attachment_ids": [str(uuid.uuid4())]},
    )
    # допущение: несуществующее вложение — отказ 4xx либо молча не используется.
    if r.status_code == 200:
        for event in sse_events(r.text):
            answer = event.get("answer") or {}
            assert all(
                s.get("kind") != "attachment" for s in answer.get("sources") or []
            )
    else:
        assert 400 <= r.status_code < 500, r.text


# ===========================================================================
# «Поделиться диалогом» (ТЗ §6)
# ===========================================================================


async def test_shared_link_shows_snapshot_of_that_conversation_to_colleague(
    kronto: Kronto,
) -> None:
    """ТЗ §6: «поделиться диалогом» с коллегой по ссылке внутри компании — коллега
    видит только этот диалог (снимок на момент публикации, только чтение)."""
    admin = await make_admin(kronto, "alfa")
    owner = await join_employee(kronto, admin, "anna@alfa-corp.ru", "Анна", "Иванова")
    colleague = await join_employee(
        kronto, admin, "boris@alfa-corp.ru", "Борис", "Смирнов"
    )
    private = await ask(owner, "Жасмин: когда меня повысят?")
    shared = await ask(owner, "Где получить справку с места работы?")

    r = await owner.post(f"{API}/conversations/{shared.conversation_id}/share")
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    conversation = await get_conversation(owner, shared.conversation_id)
    assert conversation["shared"] is True and conversation["share"]["token"] == token
    summary = next(
        c for c in await list_conversations(owner) if c["id"] == shared.conversation_id
    )
    assert summary["shared"] is True

    r = await colleague.get(f"{API}/conversations/shared/{token}")
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["title"] == conversation["title"]
    assert [m["content"] for m in view["messages"]][
        0
    ] == "Где получить справку с места работы?"
    assert len(view["messages"]) == 2
    assert "Анна" in view["owner_name"]  # допущение: автор подписан именем
    assert_no_leak(r.text, "Жасмин", private.conversation_id)

    r = await colleague.get(f"{API}/conversations/{shared.conversation_id}")
    assert r.status_code in DENIED, r.text

    await ask(
        owner,
        "А где поставить печать?",
        conversation_id=shared.conversation_id,
        parent_id=shared.answer_id,
    )
    r = await colleague.get(f"{API}/conversations/shared/{token}")
    assert len(r.json()["messages"]) == 2  # снимок не меняется сам
    assert "печать" not in r.text

    r = await owner.post(f"{API}/conversations/{shared.conversation_id}/share")
    assert r.status_code == 200 and r.json()["token"] == token  # схема: «ссылка та же»
    r = await colleague.get(f"{API}/conversations/shared/{token}")
    assert len(r.json()["messages"]) == 4

    r = await colleague.get(f"{API}/conversations/shared/{'A' * 43}")
    assert r.status_code == 404, r.text


async def test_shared_link_stops_working_after_revoke_and_delete(
    kronto: Kronto,
) -> None:
    """ТЗ §6: ссылку на диалог можно закрыть — после отзыва (и после удаления
    диалога) она больше не открывается."""
    admin = await make_admin(kronto, "alfa")
    owner = await join_employee(kronto, admin, "anna@alfa-corp.ru", "Анна", "Иванова")
    colleague = await join_employee(
        kronto, admin, "boris@alfa-corp.ru", "Борис", "Смирнов"
    )
    reply = await ask(owner, "Где получить справку с места работы?")
    base = f"{API}/conversations/{reply.conversation_id}"

    r = await owner.post(f"{base}/share")
    token = r.json()["token"]
    r = await colleague.get(f"{API}/conversations/shared/{token}")
    assert r.status_code == 200

    r = await colleague.delete(f"{base}/share")
    assert r.status_code in DENIED, r.text
    r = await colleague.get(f"{API}/conversations/shared/{token}")
    assert r.status_code == 200

    r = await owner.delete(f"{base}/share")
    assert r.status_code == 204, r.text
    r = await colleague.get(f"{API}/conversations/shared/{token}")
    assert r.status_code in (403, 404, 410), r.text  # допущение: код не задан
    assert "справку" not in r.text
    conversation = await get_conversation(owner, reply.conversation_id)
    assert conversation["shared"] is False and conversation["share"] is None

    r = await owner.post(f"{base}/share")
    assert r.status_code == 200, r.text
    token2 = r.json()["token"]
    r = await colleague.get(f"{API}/conversations/shared/{token2}")
    assert r.status_code == 200

    r = await owner.delete(base)
    assert r.status_code == 204, r.text
    for t in {token, token2}:
        r = await colleague.get(f"{API}/conversations/shared/{t}")
        assert r.status_code in (403, 404, 410), r.text
        assert "справку" not in r.text


async def test_shared_link_is_for_same_company_only(kronto: Kronto) -> None:
    """ТЗ §6: ссылка на диалог — для коллег внутри компании: человеку из другой
    компании и гостю без входа диалог не показывается."""
    admin_a = await make_admin(kronto, "alfa")
    admin_b = await make_admin(kronto, "beta")
    owner = await join_employee(kronto, admin_a, "anna@alfa-corp.ru", "Анна", "Иванова")
    reply = await ask(owner, "Где получить справку с места работы?")
    r = await owner.post(f"{API}/conversations/{reply.conversation_id}/share")
    token = r.json()["token"]

    r = await admin_a.get(f"{API}/conversations/shared/{token}")
    assert r.status_code == 200, r.text  # коллега по компании (админ) — открывает

    r = await admin_b.get(f"{API}/conversations/shared/{token}")
    assert r.status_code in DENIED, r.text
    assert_no_leak(r.text, "справку", "Иванова")

    guest = kronto.browser()
    r = await guest.get(f"{API}/conversations/shared/{token}")
    assert r.status_code in (401, 403, 404), r.text
    assert_no_leak(r.text, "справку", "Иванова")


async def test_shared_conversation_hides_restricted_sources_from_other_department(
    kronto: Kronto,
) -> None:
    """ТЗ §5, §6: поделились диалогом, где ответ опирается на документ закрытой
    папки, — коллега из другого отдела не видит фрагмент этого документа."""
    setup = await setup_restricted(kronto)
    insider, outsider = setup.insider, setup.outsider
    assert insider is not None and outsider is not None

    reply = await ask(insider, SECRET_QUESTION)
    assert reply.origin == "documents" and setup.secret_id in reply.material_ids
    r = await insider.post(f"{API}/conversations/{reply.conversation_id}/share")
    assert r.status_code == 200, r.text
    token = r.json()["token"]

    r = await outsider.get(f"{API}/conversations/shared/{token}")
    assert r.status_code == 200, r.text
    for message in r.json()["messages"]:
        for source in message["sources"]:
            if source["material_id"] == setup.secret_id:
                # схема: content=null — документ недоступен смотрящему.
                assert source["content"] is None, source
            assert "7931" not in (source["content"] or "")
    # Разбор 06.10: и сам ответ, пересказывающий закрытый документ, скрыт —
    # иначе скрытый фрагмент источника ничего не защищал.
    answers = [m for m in r.json()["messages"] if m["role"] == "assistant"]
    assert answers and all("7931" not in m["content"] for m in answers), answers

    r = await setup.admin.get(f"{API}/conversations/shared/{token}")
    assert r.status_code == 200, r.text
    admin_sources = [s for m in r.json()["messages"] for s in m["sources"]]
    assert any(
        s["material_id"] == setup.secret_id and s["content"] for s in admin_sources
    ), admin_sources
    admin_answers = [m for m in r.json()["messages"] if m["role"] == "assistant"]
    assert any("7931" in m["content"] for m in admin_answers), admin_answers


# ===========================================================================
# Подсказки вопросов (ТЗ §6)
# ===========================================================================


async def test_admin_suggestions_shown_to_employees_of_company_only(
    kronto: Kronto,
) -> None:
    """ТЗ §6: подсказки на пустом экране, заданные админом, видят сотрудники его
    компании (в заданном порядке); сотрудник подсказки не задаёт; чужая компания
    их не видит."""
    admin_a = await make_admin(kronto, "alfa")
    admin_b = await make_admin(kronto, "beta")
    employee = await join_employee(kronto, admin_a, "ivan@alfa-corp.ru")

    r = await admin_a.post(f"{API}/suggestions", json={"text": "Как оформить отпуск?"})
    assert r.status_code == 201, r.text
    first = r.json()
    r = await admin_a.post(f"{API}/suggestions", json={"text": "Где взять пропуск?"})
    assert r.status_code == 201, r.text
    second = r.json()

    r = await employee.get(f"{API}/suggestions")
    assert r.status_code == 200, r.text
    # допущение: новая подсказка встаёт в конец списка.
    assert [s["id"] for s in r.json()["company"]] == [first["id"], second["id"]]

    r = await admin_a.put(
        f"{API}/suggestions/order", json={"ids": [second["id"], first["id"]]}
    )
    assert r.status_code == 200, r.text
    r = await employee.get(f"{API}/suggestions")
    assert [s["id"] for s in r.json()["company"]] == [second["id"], first["id"]]

    r = await admin_a.patch(
        f"{API}/suggestions/{first['id']}", json={"text": "Как взять отгул?"}
    )
    assert r.status_code == 200 and r.json()["text"] == "Как взять отгул?"
    r = await admin_a.delete(f"{API}/suggestions/{second['id']}")
    assert r.status_code == 204, r.text
    r = await employee.get(f"{API}/suggestions")
    assert [s["text"] for s in r.json()["company"]] == ["Как взять отгул?"]

    r = await employee.post(f"{API}/suggestions", json={"text": "Моя подсказка"})
    assert r.status_code in DENIED, r.text
    r = await employee.delete(f"{API}/suggestions/{first['id']}")
    assert r.status_code in DENIED, r.text
    for bad in ("", "   ", "п" * 201):
        r = await admin_a.post(f"{API}/suggestions", json={"text": bad})
        assert r.status_code == 422, (bad[:5], r.status_code)

    r = await admin_b.get(f"{API}/suggestions")
    assert r.status_code == 200 and r.json()["company"] == []
    assert "отгул" not in r.text
    r = await admin_b.delete(f"{API}/suggestions/{first['id']}")
    assert r.status_code in DENIED, r.text


async def test_frequent_question_of_one_person_is_not_suggested(kronto: Kronto) -> None:
    """ТЗ §6: частые вопросы в подсказках — обезличенно (схема: не меньше трёх
    разных людей); вопрос одного человека, даже заданный много раз, не
    показывается."""
    admin = await make_admin(kronto, "alfa")
    employee = await join_employee(kronto, admin, "ivan@alfa-corp.ru")
    question = "Как продлить пропуск в серверную?"
    for _ in range(3):
        assert (await ask(employee, question)).ok
    await kronto.run_background()

    for viewer in (employee, admin):
        r = await viewer.get(f"{API}/suggestions")
        assert r.status_code == 200, r.text
        assert all("серверную" not in f.lower() for f in r.json()["frequent"]), r.text
    r = await admin.get(f"{API}/analytics")
    assert r.status_code == 200, r.text
    assert all("серверную" not in f["question"].lower() for f in r.json()["frequent"])


async def test_frequent_question_of_three_people_is_suggested(kronto: Kronto) -> None:
    """ТЗ §6: подсказки на пустом экране — частые вопросы компании (схема: заданные
    не меньше чем тремя разными людьми), без имён."""
    admin = await make_admin(kronto, "alfa")
    first = await join_employee(kronto, admin, "anna@alfa-corp.ru", "Анна", "Иванова")
    second = await join_employee(
        kronto, admin, "boris@alfa-corp.ru", "Борис", "Смирнов"
    )
    question = "Где взять пропуск на парковку?"
    # Разбор 06.10: в подсказки попадают вопросы, на которые нашёлся ответ в
    # документах (схема API теперь это описывает) — нужен документ.
    r = await upload_doc(
        admin,
        "Парковка",
        "parkovka.txt",
        "Где взять пропуск на парковку: пропуск на парковку выдаёт охрана "
        "на первом этаже.".encode(),
    )
    assert r.status_code == 201, r.text
    await kronto.run_background()
    # допущение: администратор — тоже один из «трёх разных людей» компании.
    for person in (admin, first, second):
        assert (await ask(person, question)).ok
    # допущение: частые вопросы считаются без ночной задачи (воркер — достаточно).
    await kronto.run_background()

    r = await first.get(f"{API}/suggestions")
    assert r.status_code == 200, r.text
    frequent = [f.lower() for f in r.json()["frequent"]]
    assert any("пропуск" in f and "парковку" in f for f in frequent), frequent
    assert_no_leak(r.text, "Анна", "Борис", first.email, second.email)

    r = await admin.get(f"{API}/analytics")
    assert r.status_code == 200, r.text
    rows = [f for f in r.json()["frequent"] if "парковку" in f["question"].lower()]
    assert rows and rows[0]["people"] >= 3, r.text
    assert_no_leak(r.text, "Иванова", "Смирнов", first.email, second.email)


async def test_employee_cannot_self_join_department_with_closed_folder(
    kronto: Kronto,
) -> None:
    """ТЗ §5, §7: закрытая папка — только отделам, которым она открыта.
    Добавлен при разборе 06.10: сотрудник выбирал себе отдел в профиле и
    так открывал себе чужую закрытую папку. В такой отдел записывает
    администратор; в обычный отдел — по-прежнему сам."""
    r = await setup_restricted(kronto, insider=False)
    outsider = r.outsider
    assert outsider is not None
    me = await outsider.get(f"{API}/auth/me")
    member = me.json()["company"]["member_id"]

    refused = await outsider.patch(
        f"{API}/people/{member}", json={"department_id": r.dept_in}
    )
    assert refused.status_code in DENIED, refused.text
    await assert_secret_hidden_from(outsider, r.secret_id)

    plain = await make_department(r.admin, "Склад")
    ok = await outsider.patch(f"{API}/people/{member}", json={"department_id": plain})
    assert ok.status_code == 200, ok.text

    by_admin = await r.admin.patch(
        f"{API}/people/{member}", json={"department_id": r.dept_in}
    )
    assert by_admin.status_code == 200, by_admin.text
    await assert_secret_visible_to(outsider, r.secret_id)
