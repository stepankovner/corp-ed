"""Заявки на созвон со страницы тарифов (досье 3.3 и 10.1, решение 28.09)."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.api.v1.dependencies import get_lead_service
from corp_ed.cli import _parser
from corp_ed.core.config import LeadSettings
from corp_ed.domain.leads import CALL_SLOTS, CALL_TIMEZONE, LeadStatus
from corp_ed.domain.models import Lead
from corp_ed.main import app
from corp_ed.repositories.lead_repository import LeadRepository
from corp_ed.services.lead_service import LeadService
from corp_ed.services.retention_service import RetentionService

POLICY = "2026-09-28"
OPEN = LeadSettings(
    enabled=True, policy_url="https://kronto.example/privacy", policy_version=POLICY
)


def _next_workday(offset: int = 1) -> date:
    day = datetime.now(CALL_TIMEZONE).date() + timedelta(days=offset)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _next_weekend() -> date:
    day = datetime.now(CALL_TIMEZONE).date() + timedelta(days=1)
    while day.weekday() < 5:
        day += timedelta(days=1)
    return day


def _body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "company_name": "ООО «Меридиан Строй»",
        "contact_name": "Анна Смирнова",
        "phone": "+7 (999) 123-45-67",
        "email": "Anna@Meridian-Stroy.ru",
        "seats": 60,
        "tariff": "base",
        "preferred_date": _next_workday().isoformat(),
        "preferred_slot": CALL_SLOTS[1],
        "comment": "Документы в Битрикс24 и на сетевой папке",
        "policy_version": POLICY,
        "consent": True,
    }
    body.update(overrides)
    return body


@pytest.fixture
def leads_open(api: httpx.AsyncClient, session: AsyncSession) -> Iterator[None]:
    app.dependency_overrides[get_lead_service] = lambda: LeadService(
        LeadRepository(session), session, OPEN
    )
    yield
    app.dependency_overrides.pop(get_lead_service, None)


async def _leads(session: AsyncSession) -> list[Lead]:
    return list(await session.scalars(select(Lead)))


# --- форма выключена по умолчанию ------------------------------------------------


async def test_form_is_closed_until_policy_is_set(api: httpx.AsyncClient) -> None:
    form = await api.get("/api/v1/leads/form")

    assert form.status_code == 200
    body = form.json()
    assert (body["enabled"], body["policy_url"], body["policy_version"]) == (
        False,
        None,
        None,
    )
    assert body["slots"] == list(CALL_SLOTS)
    today = datetime.now(CALL_TIMEZONE).date()
    assert body["first_date"] == (today + timedelta(days=1)).isoformat()
    assert body["last_date"] == (today + timedelta(days=30)).isoformat()
    assert body["timezone"] == "Europe/Moscow"

    submit = await api.post("/api/v1/leads", json=_body())
    assert submit.status_code == 404
    assert "не открыта" in submit.json()["detail"]


def test_enabling_requires_policy() -> None:
    with pytest.raises(ValidationError, match="LEADS_POLICY_URL"):
        LeadSettings(enabled=True)
    with pytest.raises(ValidationError, match="https"):
        LeadSettings(policy_url="http://kronto.example/privacy")
    assert LeadSettings(policy_url="/privacy").policy_url == "/privacy"


# --- приём заявки ------------------------------------------------------------------


async def test_lead_is_stored_with_consent(
    api: httpx.AsyncClient, session: AsyncSession, leads_open: None
) -> None:
    form = (await api.get("/api/v1/leads/form")).json()
    assert (form["enabled"], form["policy_version"]) == (True, POLICY)

    response = await api.post("/api/v1/leads", json=_body())

    assert response.status_code == 201
    assert response.json() == {"status": "received"}
    [lead] = await _leads(session)
    assert lead.phone == "+79991234567"
    assert lead.email == "anna@meridian-stroy.ru"
    assert (lead.seats, lead.tariff, lead.status) == (60, "base", "new")
    assert lead.preferred_slot == CALL_SLOTS[1]
    assert lead.policy_version == POLICY
    assert datetime.now(UTC) - lead.consented_at < timedelta(minutes=1)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"consent": False}, "согласие"),
        ({"policy_version": "2026-01-01"}, "обновилась"),
        ({"preferred_slot": "03:00–05:00"}, "время"),
        ({"preferred_date": datetime.now(CALL_TIMEZONE).date().isoformat()}, "Дата"),
        ({"preferred_date": _next_workday(40).isoformat()}, "Дата"),
        ({"preferred_date": _next_weekend().isoformat()}, "будним"),
    ],
)
async def test_lead_checks(
    api: httpx.AsyncClient,
    session: AsyncSession,
    leads_open: None,
    overrides: dict[str, object],
    message: str,
) -> None:
    response = await api.post("/api/v1/leads", json=_body(**overrides))

    assert response.status_code == 422
    assert message in response.json()["detail"]
    assert await _leads(session) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"phone": "12345"},
        {"phone": "позвоните мне"},
        {"seats": 0},
        {"email": "not-an-email"},
        {"tariff": "free"},
        {"company_name": " "},
        {"status": "scheduled"},
    ],
)
async def test_lead_fields_are_validated(
    api: httpx.AsyncClient, leads_open: None, overrides: dict[str, object]
) -> None:
    assert (await api.post("/api/v1/leads", json=_body(**overrides))).status_code == 422


async def test_email_and_comment_are_optional(
    api: httpx.AsyncClient, session: AsyncSession, leads_open: None
) -> None:
    body = _body(email=None, comment=None, tariff="custom")

    assert (await api.post("/api/v1/leads", json=body)).status_code == 201
    [lead] = await _leads(session)
    assert (lead.email, lead.comment, lead.tariff) == (None, None, "custom")


async def test_honeypot_is_answered_but_not_stored(
    api: httpx.AsyncClient, session: AsyncSession, leads_open: None
) -> None:
    response = await api.post(
        "/api/v1/leads", json=_body(website="https://spam.example")
    )

    assert response.status_code == 201
    assert await _leads(session) == []


async def test_leads_are_rate_limited_per_ip(
    api: httpx.AsyncClient, leads_open: None
) -> None:
    for _ in range(5):
        assert (await api.post("/api/v1/leads", json=_body())).status_code == 201

    response = await api.post("/api/v1/leads", json=_body())

    assert response.status_code == 429


# --- команда: CLI и срок хранения ---------------------------------------------------


async def test_team_lists_and_marks_leads(
    api: httpx.AsyncClient, session: AsyncSession, leads_open: None
) -> None:
    await api.post("/api/v1/leads", json=_body(company_name="Первая"))
    await api.post("/api/v1/leads", json=_body(company_name="Вторая"))
    service = LeadService(LeadRepository(session), session, OPEN)

    fresh = await service.list_recent(status=LeadStatus.NEW, limit=50)
    assert {lead.company_name for lead in fresh} == {"Первая", "Вторая"}

    await service.set_status(fresh[0].id, LeadStatus.CONTACTED)
    remaining = await service.list_recent(status=LeadStatus.NEW, limit=50)
    assert len(remaining) == 1
    everything = await service.list_recent(status=None, limit=50)
    assert len(everything) == 2


def test_cli_leads_arguments() -> None:
    listing = _parser().parse_args(["leads", "list"])
    assert (listing.leads_command, listing.status, listing.limit) == ("list", "new", 50)
    marking = _parser().parse_args(
        [
            "leads",
            "set-status",
            "--id",
            "0b6f7c1e-2f5a-4a51-9d1e-1f7a2d3c4b5a",
            "--status",
            "rejected",
        ]
    )
    assert marking.status == "rejected"
    with pytest.raises(SystemExit):
        _parser().parse_args(["leads", "list", "--status", "lost"])


async def test_old_leads_are_purged(
    session: AsyncSession, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    old = Lead(
        company_name="Старая",
        contact_name="Иван",
        phone="+79990000000",
        seats=30,
        tariff="base",
        preferred_date=date(2026, 1, 12),
        preferred_slot=CALL_SLOTS[0],
        policy_version=POLICY,
        consented_at=datetime.now(UTC),
        created_at=datetime.now(UTC) - timedelta(days=181),
    )
    fresh = Lead(
        company_name="Свежая",
        contact_name="Анна",
        phone="+79991111111",
        seats=30,
        tariff="base",
        preferred_date=date(2026, 10, 12),
        preferred_slot=CALL_SLOTS[0],
        policy_version=POLICY,
        consented_at=datetime.now(UTC),
    )
    session.add_all([old, fresh])
    await session.commit()

    report = await RetentionService(
        session_maker, qa_log_days=90, lead_days=180
    ).purge()

    assert report.leads == 1
    session.expire_all()
    assert [lead.company_name for lead in await _leads(session)] == ["Свежая"]
