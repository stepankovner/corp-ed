"""Сценарии этапов 1–10 для проверки стенда (`python -m corp_ed.stand check`).

Идут после основного сценария, пока документ проверки ещё загружен:

- чат (ТЗ §6): ответ в диалоге потоком, диалог в списке, ссылка
  «поделиться», удаление;
- уведомления и первые шаги (§8), обзор и настройки компании (§7),
  сеансы входа (§3);
- отдел и закрытая папка (§5): документ папки сотрудник не видит, пока
  он не в отделе папки;
- приглашение и вступление (§2): вторая служебная учётка вступает по коду
  и отвечает как сотрудник; после проверки её убирают из компании;
- песочница сайта (§1): ответ по документам вымышленной компании;
- загрузка файлов Word (docx и doc), PDF, Excel и PowerPoint: текст из
  них достаёт песочница извлечения на сервере, чего документ .md
  основного сценария не проверяет.

Всё, что сценарий создал, он удаляет — повторные запуски не копят данные.
Учётка сотрудника необязательна: без неё шаги сотрудника пропускаются.
"""

import io
import json
import secrets
import time
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from corp_ed.stand import (
    API,
    Hook,
    Report,
    StandClient,
    StandError,
    _ask,
    _code,
    _wait_ready,
    _yes,
)

DEMO_QUESTION = "Как оформить командировку на объект?"
DEMO_SOURCE = "Положение о служебных командировках"
DEMO_DOCUMENTS = 7
DEMO_ATTEMPTS = 3
"""Сразу после выкатки документы песочницы могут ещё индексироваться."""
DEMO_RETRY_SECONDS = 20.0


@dataclass(frozen=True)
class Credentials:
    email: str
    password: str
    totp_secret: str | None = None
    new_password: str | None = None
    """Первый вход с временным паролем: на этот сценарий сменит его."""


@dataclass(frozen=True)
class SmokeDocument:
    material_id: str
    title: str
    question: str
    nonce: str


def folder_document(nonce: str) -> tuple[str, str, str, str]:
    """Название, текст, вопрос и код из документа закрытой папки."""
    code = f"СКЛ-{secrets.token_hex(3).upper()}"
    title = f"Склад отдела проверки {nonce}"
    text = (
        f"# Склад отдела проверки {nonce}\n\n"
        f"Код двери склада отдела проверки {nonce} — {code}. Его знают "
        "только сотрудники отдела проверки.\n"
    )
    return title, text, f"Какой код двери склада отдела проверки {nonce}?", code


_DOCX_PARTS = {
    "[Content_Types].xml": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/'
        'vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    ),
    "_rels/.rels": (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
        'relationships"><Relationship Id="rId1" Type="http://schemas.'
        'openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    ),
}


def _docx(paragraphs: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document '
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, xml in _DOCX_PARTS.items():
            archive.writestr(name, xml)
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def _pdf(lines: list[str]) -> bytes:
    """Одна страница, встроенный шрифт Helvetica — без кириллицы, зато без
    библиотек: проверяется извлечение на сервере, а не сборка файла."""
    text = "".join(
        f"BT /F1 12 Tf 72 {720 - 24 * i} Td ({line}) Tj ET\n"
        for i, line in enumerate(lines)
    ).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n%sendstream" % (len(text), text),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_OFFICE = "application/vnd.openxmlformats-officedocument"


def _zip(parts: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, xml in parts.items():
            archive.writestr(name, '<?xml version="1.0" encoding="UTF-8"?>' + xml)
    return buffer.getvalue()


def _package(main: str, main_type: str, extra: dict[str, str]) -> dict[str, str]:
    """[Content_Types].xml и связь пакета с главной частью."""
    overrides = "".join(
        f'<Override PartName="/{name}" ContentType="{_OFFICE}.{kind}"/>'
        for name, kind in {main: main_type, **extra}.items()
    )
    return {
        "[Content_Types].xml": (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
            'content-types"><Default Extension="rels" ContentType="application/'
            'vnd.openxmlformats-package.relationships+xml"/><Default '
            f'Extension="xml" ContentType="application/xml"/>{overrides}</Types>'
        ),
        "_rels/.rels": (
            f'<Relationships xmlns="{_PKG}"><Relationship Id="rId1" '
            f'Type="{_REL}/officeDocument" Target="{main}"/></Relationships>'
        ),
    }


def _xlsx(rows: list[list[str]]) -> bytes:
    """Один лист, строки — встроенным текстом (без общей таблицы строк)."""
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    body = "".join(
        f'<row r="{r}">'
        + "".join(
            f'<c r="{chr(65 + c)}{r}" t="inlineStr"><is><t>{text}</t></is></c>'
            for c, text in enumerate(cells)
        )
        + "</row>"
        for r, cells in enumerate(rows, start=1)
    )
    return _zip(
        {
            **_package(
                "xl/workbook.xml",
                "spreadsheetml.sheet.main+xml",
                {"xl/worksheets/sheet1.xml": "spreadsheetml.worksheet+xml"},
            ),
            "xl/workbook.xml": (
                f'<workbook xmlns="{main}" xmlns:r="{_REL}"><sheets><sheet '
                'name="Лист1" sheetId="1" r:id="rId1"/></sheets></workbook>'
            ),
            "xl/_rels/workbook.xml.rels": (
                f'<Relationships xmlns="{_PKG}"><Relationship Id="rId1" '
                f'Type="{_REL}/worksheet" Target="worksheets/sheet1.xml"/>'
                "</Relationships>"
            ),
            "xl/worksheets/sheet1.xml": (
                f'<worksheet xmlns="{main}"><sheetData>{body}</sheetData></worksheet>'
            ),
        }
    )


def _pptx(heading: str, lines: list[str]) -> bytes:
    """Один слайд: заголовок и текст."""
    ns = (
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        f'xmlns:r="{_REL}"'
    )

    def shape(paragraphs: list[str], placeholder: str) -> str:
        text = "".join(
            f"<a:p><a:r><a:t>{line}</a:t></a:r></a:p>" for line in paragraphs
        )
        return (
            '<p:sp><p:nvSpPr><p:cNvPr id="2" name="s"/><p:cNvSpPr/><p:nvPr>'
            f"{placeholder}</p:nvPr></p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/>"
            f"{text}</p:txBody></p:sp>"
        )

    return _zip(
        {
            **_package(
                "ppt/presentation.xml",
                "presentationml.presentation.main+xml",
                {"ppt/slides/slide1.xml": "presentationml.slide+xml"},
            ),
            "ppt/presentation.xml": (
                f'<p:presentation {ns}><p:sldIdLst><p:sldId id="256" r:id="rId1"/>'
                "</p:sldIdLst></p:presentation>"
            ),
            "ppt/_rels/presentation.xml.rels": (
                f'<Relationships xmlns="{_PKG}"><Relationship Id="rId1" '
                f'Type="{_REL}/slide" Target="slides/slide1.xml"/></Relationships>'
            ),
            "ppt/slides/slide1.xml": (
                f'<p:sld {ns}><p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" '
                'name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
                + shape([heading], '<p:ph type="title"/>')
                + shape(lines, "")
                + "</p:spTree></p:cSld></p:sld>"
            ),
        }
    )


_DOC_TEMPLATE = Path(__file__).parent / "stand_files" / "check.doc"
_DOC_NONCE = "QQQQQQ".encode("utf-16-le")
"""Место кода проверки в check.doc: в .doc нет контрольных сумм, и код той
же длины встаёт на место без пересборки файла (собран
tests/ingest/doc_samples.py, как Word 97–2003: OLE, FIB, куски текста)."""


def _doc(nonce: str) -> bytes:
    return _DOC_TEMPLATE.read_bytes().replace(_DOC_NONCE, nonce.encode("utf-16-le"))


def format_files(nonce: str) -> list[tuple[str, bytes, str]]:
    """Файлы всех форматов, кроме .md: имя, содержимое, название документа.
    nonce — шесть знаков (место под него в check.doc — шесть)."""
    return [
        (
            f"check-{nonce}.docx",
            _docx(
                [
                    f"Порядок выдачи пропусков {nonce}",
                    f"Пропуск проверки {nonce} выдаёт охрана на первом этаже.",
                ]
            ),
            f"Проверка Word {nonce}",
        ),
        (
            f"check-{nonce}.pdf",
            _pdf(
                [
                    f"Parking rules {nonce}",
                    f"Check parking {nonce}: level minus two, places 10 to 20.",
                ]
            ),
            f"Проверка PDF {nonce}",
        ),
        (f"check-{nonce}.doc", _doc(nonce), f"Проверка Word 97 {nonce}"),
        (
            f"check-{nonce}.xlsx",
            _xlsx(
                [
                    ["Кабинет", "Ответственный"],
                    [f"Переговорная проверки {nonce}", "Отдел проверки"],
                ]
            ),
            f"Проверка Excel {nonce}",
        ),
        (
            f"check-{nonce}.pptx",
            _pptx(
                f"Порядок дежурств {nonce}",
                [f"Дежурный проверки {nonce} меняется каждый понедельник."],
            ),
            f"Проверка PowerPoint {nonce}",
        ),
    ]


async def stream_chat(client: StandClient, question: str) -> dict[str, Any]:
    """Новый диалог: события потока до done или error."""
    events: list[dict[str, Any]] = []
    async with client.http.stream(
        "POST",
        f"{API}/conversations",
        json={"question": question},
        headers=client._headers(),
    ) as response:
        if response.status_code != 200:
            await response.aread()
            raise StandError(f"чат: HTTP {response.status_code} {_code(response)}")
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    start = next((e for e in events if e.get("type") == "start"), None)
    end = next((e for e in events if e.get("type") in ("done", "error")), None)
    if start is None or end is None:
        raise StandError(f"чат: поток без начала или конца ({len(events)} событий)")
    return {
        "conversation_id": start["conversation"]["id"],
        "end": end,
        "deltas": sum(1 for e in events if e.get("type") == "delta"),
    }


async def run_scenarios(
    client: StandClient,
    report: Report,
    smoke: SmokeDocument,
    *,
    employee: Credentials | None,
    timeout: float,
    poll_interval: float,
    before_poll: Hook | None,
    sleep: Callable[[float], Awaitable[None]],
) -> None:
    """Шаги этапов 1–10 от имени администратора (client) и, если задана,
    второй учётки. Ошибка шага не прерывает остальные, уборка — всегда."""
    created: dict[str, str] = {}
    staff = StandClient(client.http)
    try:
        await _chat(client, report, smoke)
        await _overview(client, report)
        await _folder(
            client,
            report,
            smoke,
            created,
            employee=employee,
            staff=staff,
            timeout=timeout,
            poll_interval=poll_interval,
            before_poll=before_poll,
            sleep=sleep,
        )
        await _logout(staff, report)
        await _sandbox(client, report, before_poll=before_poll, sleep=sleep)
        await _formats(
            client,
            report,
            created,
            timeout=timeout,
            poll_interval=poll_interval,
            before_poll=before_poll,
            sleep=sleep,
        )
    except StandError as exc:
        report.add("сценарии этапов прерваны", False, str(exc))
    finally:
        await _clean(client, report, created)


async def _chat(client: StandClient, report: Report, smoke: SmokeDocument) -> None:
    started = time.monotonic()
    result = await stream_chat(client, smoke.question)
    end = result["end"]
    answer = end.get("answer") or {}
    titles = [str(s.get("title")) for s in answer.get("sources") or []]
    named = smoke.nonce in str(answer.get("content", ""))
    report.add(
        "чат: ответ потоком",
        end.get("type") == "done"
        and answer.get("origin") == "documents"
        and smoke.title in titles
        and named
        and result["deltas"] > 0,
        f"{end.get('type')}, origin {answer.get('origin')}, кусков текста "
        f"{result['deltas']}, документ среди источников: "
        f"{_yes(smoke.title in titles)}, кодовое слово: {_yes(named)}, "
        f"{time.monotonic() - started:.0f} с",
    )
    conversation = result["conversation_id"]
    listed = await client.request("GET", "/conversations")
    in_list = listed.status_code == 200 and any(
        item.get("id") == conversation for item in listed.json().get("items", [])
    )
    shared = await client.request("POST", f"/conversations/{conversation}/share")
    token = shared.json().get("token") if shared.status_code == 200 else None
    opened = (
        await client.request("GET", f"/conversations/shared/{token}") if token else None
    )
    unshared = await client.request("DELETE", f"/conversations/{conversation}/share")
    deleted = await client.request("DELETE", f"/conversations/{conversation}")
    report.add(
        "чат: список, «поделиться», удаление",
        in_list
        and opened is not None
        and opened.status_code == 200
        and unshared.status_code == 204
        and deleted.status_code == 204,
        f"в списке: {_yes(in_list)}, ссылка: HTTP {shared.status_code}"
        f"/{opened.status_code if opened else '—'}, закрыта: HTTP "
        f"{unshared.status_code}, удалён: HTTP {deleted.status_code}",
    )


async def _overview(client: StandClient, report: Report) -> None:
    notifications = await client.request("GET", "/notifications")
    onboarding = await client.request("GET", "/onboarding")
    steps = onboarding.json() if onboarding.status_code == 200 else {}
    report.add(
        "уведомления и первые шаги",
        notifications.status_code == 200
        and "unread" in notifications.json()
        and steps.get("documents") is True,
        f"уведомления HTTP {notifications.status_code}, первые шаги HTTP "
        f"{onboarding.status_code}, документы загружены: "
        f"{_yes(steps.get('documents') is True)}",
    )
    analytics = await client.request("GET", "/analytics", params={"days": 7})
    company = await client.request("GET", "/company")
    questions = (
        analytics.json().get("questions", 0) if analytics.status_code == 200 else 0
    )
    report.add(
        "обзор и настройки компании",
        analytics.status_code == 200 and questions >= 1 and company.status_code == 200,
        f"вопросов за 7 дней: {questions}, настройки HTTP {company.status_code}",
    )
    sessions = await client.request("GET", "/auth/sessions")
    current = sessions.status_code == 200 and any(
        item.get("current") for item in sessions.json()
    )
    report.add(
        "сеансы входа",
        current,
        f"HTTP {sessions.status_code}, текущий сеанс в списке: {_yes(current)}",
    )


async def _folder(
    client: StandClient,
    report: Report,
    smoke: SmokeDocument,
    created: dict[str, str],
    *,
    employee: Credentials | None,
    staff: StandClient,
    timeout: float,
    poll_interval: float,
    before_poll: Hook | None,
    sleep: Callable[[float], Awaitable[None]],
) -> None:
    nonce = secrets.token_hex(3)
    department = await client.request(
        "POST", "/departments", json={"name": f"Отдел проверки {nonce}"}
    )
    if department.status_code != 201:
        raise StandError(f"отдел: HTTP {department.status_code} {_code(department)}")
    created["department"] = department.json()["id"]
    folder = await client.request(
        "POST",
        "/folders",
        json={
            "name": f"Склад проверки {nonce}",
            "restricted": True,
            "department_ids": [created["department"]],
        },
    )
    if folder.status_code != 201:
        raise StandError(f"папка: HTTP {folder.status_code} {_code(folder)}")
    created["folder"] = folder.json()["id"]
    title, text, question, code = folder_document(nonce)
    material = await client.request(
        "POST",
        "/materials",
        json={"title": title, "content": text, "folder_id": created["folder"]},
    )
    if material.status_code != 201:
        raise StandError(
            f"документ папки: HTTP {material.status_code} {_code(material)}"
        )
    created["folder_material"] = material.json()["id"]
    ready = await _wait_ready(
        client,
        created["folder_material"],
        timeout=timeout,
        poll_interval=poll_interval,
        before_poll=before_poll,
        sleep=sleep,
    )
    report.add(
        "отдел и закрытая папка",
        ready["status"] == "ready",
        f"документ в папке отдела: {ready['status']}",
    )
    if employee is None:
        report.add(
            "сотрудник по приглашению", True, "пропущено: учётка сотрудника не задана"
        )
        return

    invite = await client.request("POST", "/invites", json={})
    if invite.status_code != 201:
        raise StandError(f"приглашение: HTTP {invite.status_code} {_code(invite)}")
    created["invite"] = invite.json()["invite"]["id"]
    invite_code = invite.json()["code"]

    try:
        await staff.login(employee.email, employee.password, employee.totp_secret)
    except StandError:
        # Временный пароль уже сменили в прошлый раз, а файл состояния на
        # сервере об этом не узнал (ответ потерялся) — входим постоянным.
        if not employee.new_password:
            raise
        await staff.login(employee.email, employee.new_password, employee.totp_secret)
    me = (await staff.request("GET", "/auth/me")).json()
    if me.get("must_change_password"):
        if not employee.new_password:
            raise StandError("сотрудник: временный пароль, нужен новый")
        await staff.change_password(employee.password, employee.new_password)
    joined = await staff.request(
        "POST", "/invites/accept", json={"secret": invite_code}
    )
    outcome = joined.json().get("outcome") if joined.status_code == 200 else None
    session = joined.json().get("session") if joined.status_code == 200 else None
    if session:
        staff.token = session["access_token"]
    me = (await staff.request("GET", "/auth/me")).json()
    company = me.get("company") or {}
    created["member"] = str(company.get("member_id") or "")
    report.add(
        "сотрудник по приглашению",
        outcome == "joined" and company.get("role") == "employee",
        f"вступление: {outcome or f'HTTP {joined.status_code} {_code(joined)}'}, "
        f"роль {company.get('role')}",
    )
    if outcome != "joined" or not created["member"]:
        return

    common = await _ask(staff, smoke.question)
    common_titles = [str(s.get("title")) for s in common.get("sources", [])]
    hidden = await _ask(staff, question)
    hidden_titles = [str(s.get("title")) for s in hidden.get("sources", [])]
    leaked = title in hidden_titles or code in str(hidden.get("content", ""))
    report.add(
        "сотрудник: общий документ виден, папка отдела — нет",
        smoke.title in common_titles and not leaked,
        f"общий документ в источниках: {_yes(smoke.title in common_titles)}, "
        f"документ папки виден: {_yes(leaked)}",
    )

    moved = await client.request(
        "PATCH",
        f"/people/{created['member']}",
        json={"department_id": created["department"]},
    )
    visible = await _ask(staff, question)
    visible_titles = [str(s.get("title")) for s in visible.get("sources", [])]
    found = title in visible_titles and code in str(visible.get("content", ""))
    report.add(
        "сотрудник в отделе видит папку",
        moved.status_code == 200 and found,
        f"перевод в отдел: HTTP {moved.status_code}, документ папки в "
        f"источниках и код в ответе: {_yes(found)}",
    )


async def _logout(staff: StandClient, report: Report) -> None:
    """«Выйти» действует сразу: токен доступа сотрудника после выхода —
    401, а не ещё до 15 минут работы."""
    if not staff.token:
        report.add("выход гасит токен", True, "пропущено: сотрудник не вошёл")
        return
    before = await staff.request("GET", "/auth/me")
    out = await staff.request("POST", "/auth/logout")
    after = await staff.request("GET", "/auth/me")
    staff.token = None
    report.add(
        "выход гасит токен",
        before.status_code == 200
        and out.status_code == 204
        and after.status_code == 401,
        f"до выхода HTTP {before.status_code}, выход HTTP {out.status_code}, "
        f"тот же токен после выхода HTTP {after.status_code}",
    )


async def _sandbox(
    client: StandClient,
    report: Report,
    *,
    before_poll: Hook | None,
    sleep: Callable[[float], Awaitable[None]],
) -> None:
    info = await client.request("GET", "/demo")
    if info.status_code != 200:
        report.add("песочница сайта", False, f"GET /demo: HTTP {info.status_code}")
        return
    documents = len(info.json().get("documents", []))
    body: dict[str, Any] = {}
    status = 0
    for attempt in range(DEMO_ATTEMPTS):
        if before_poll is not None:
            await before_poll()
        response = await client.request(
            "POST", "/demo/ask", json={"question": DEMO_QUESTION, "website": ""}
        )
        status = response.status_code
        body = response.json() if status == 200 else {}
        if status != 200 or body.get("origin") == "documents":
            break
        if attempt + 1 < DEMO_ATTEMPTS:
            await sleep(DEMO_RETRY_SECONDS)
    titles = [str(s.get("title")) for s in body.get("sources", [])]
    report.add(
        "песочница сайта",
        documents == DEMO_DOCUMENTS
        and body.get("origin") == "documents"
        and DEMO_SOURCE in titles,
        f"документов {documents}, ответ: HTTP {status}, origin "
        f"{body.get('origin')}, источник «{DEMO_SOURCE}»: "
        f"{_yes(DEMO_SOURCE in titles)}"
        + ("" if DEMO_SOURCE in titles else f" (источники: {'; '.join(titles)})"),
    )


async def _formats(
    client: StandClient,
    report: Report,
    created: dict[str, str],
    *,
    timeout: float,
    poll_interval: float,
    before_poll: Hook | None,
    sleep: Callable[[float], Awaitable[None]],
) -> None:
    started = time.monotonic()
    uploaded: list[tuple[str, str]] = []
    details: list[str] = []
    files = format_files(secrets.token_hex(3))
    for filename, data, title in files:
        extension = filename.rsplit(".", 1)[1]
        try:
            material = await client.upload(filename, data, title)
        except StandError as exc:
            details.append(str(exc))
            continue
        created[f"file:{extension}"] = str(material["id"])
        uploaded.append((extension, str(material["id"])))
    ready = 0
    for extension, material_id in uploaded:
        material = await _wait_ready(
            client,
            material_id,
            timeout=timeout,
            poll_interval=poll_interval,
            before_poll=before_poll,
            sleep=sleep,
        )
        ready += material["status"] == "ready"
        details.append(
            f"{extension}: {material['status']}"
            + (f" ({material['status_error']})" if material.get("status_error") else "")
        )
    report.add(
        "загрузка docx, pdf, doc, xlsx, pptx",
        ready == len(files),
        ", ".join(details) + f", {time.monotonic() - started:.0f} с",
    )


async def _clean(client: StandClient, report: Report, created: dict[str, str]) -> None:
    """Убрать за собой; не удалось — шаг красный, данные видно в отчёте."""
    if not client.token:
        return
    paths = []
    if created.get("member"):
        paths.append(("сотрудник из компании", f"/users/{created['member']}"))
    if created.get("folder_material"):
        paths.append(("документ папки", f"/materials/{created['folder_material']}"))
    if created.get("folder"):
        paths.append(("папка", f"/folders/{created['folder']}"))
    if created.get("department"):
        paths.append(("отдел", f"/departments/{created['department']}"))
    if created.get("invite"):
        paths.append(("приглашение", f"/invites/{created['invite']}"))
    for key, material_id in created.items():
        if key.startswith("file:"):
            paths.append((f"файл {key[5:]}", f"/materials/{material_id}"))
    if not paths:
        return
    results = []
    for name, path in paths:
        response = await client.request("DELETE", path)
        results.append((name, response.status_code))
    report.add(
        "уборка сценариев",
        all(status == 204 for _, status in results),
        ", ".join(f"{name}: HTTP {status}" for name, status in results),
    )
