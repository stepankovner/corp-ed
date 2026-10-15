"""Живая проверка Jira Software Server/DC: настоящая Jira в Docker с
тестовой лицензией Atlassian и выдуманной компанией.

Как поднять стенд — tests/live/jira_dc/README.md (compose, мастер,
seed.py, security.mjs, токены). Без переменных — пропуск:

    JIRA_DC_URL=http://127.0.0.1:8510 JIRA_DC_TOKEN=… \\
        uv run pytest tests/live/test_jira_dc_live.py -q

Стенд слушает http://127.0.0.1: в бою адаптер ходит только по HTTPS на
публичные адреса (core/outbound.py), здесь проверка адреса пропускает
один этот стенд — всё остальное идёт как в бою.
"""

import os
from collections.abc import Iterator

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.base import AdapterConfigError, FetchedFile, FetchedPage
from corp_ed.connectors.html import html_to_markdown
from corp_ed.connectors.jira_dc import KIND, register
from corp_ed.connectors.jira_dc.adapter import JiraAdapter, build_adapter
from corp_ed.connectors.registry import AdapterRegistry
from corp_ed.core import outbound
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.secrets import SecretBox
from corp_ed.core.security import hash_password
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import Connector, Tenant, User, UserRole
from corp_ed.domain.types import (
    ConnectorMode,
    MaterialVisibility,
    SyncRunStatus,
    SyncTrigger,
)
from corp_ed.ingest.extract import detect_format, extract
from corp_ed.services.connector_sync_service import ConnectorSyncService, SyncOutcome
from tests.factories import make_user
from tests.live.jira_dc.seed import (
    ADMIN,
    EXPECTED_READERS,
    HIDDEN_FROM_SERVICE,
    INACTIVE,
    RESTRICTED_COMMENT,
    USERS,
    Seeder,
    email,
)
from tests.test_connector_sync import access_of, materials_of

URL = os.environ.get("JIRA_DC_URL", "").rstrip("/")
TOKEN = os.environ.get("JIRA_DC_TOKEN", "")
MEMBER_TOKEN = os.environ.get("JIRA_DC_MEMBER_TOKEN", "")
ADMIN_TOKEN = os.environ.get("JIRA_DC_ADMIN_TOKEN", "")
KEY = Fernet.generate_key().decode()

pytestmark = pytest.mark.skipif(
    not (URL and TOKEN),
    reason="нужны JIRA_DC_URL и JIRA_DC_TOKEN (стенд Jira DC)",
)


@pytest.fixture
def stand_address(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Пропустить проверку адреса только для стенда (http, 127.0.0.1)."""
    original = outbound.validate_outbound_url
    stand = httpx.URL(URL)

    async def validate(
        url: str, *, resolver: outbound.Resolver | None = None
    ) -> outbound.OutboundTarget:
        target = httpx.URL(url)
        if (target.host, target.port) == (stand.host, stand.port):
            return outbound.OutboundTarget(
                url=url, host=target.host, port=target.port or 80, address=target.host
            )
        return await original(url, resolver=resolver)

    monkeypatch.setattr(outbound, "validate_outbound_url", validate)
    yield


def _settings() -> ConnectorSettings:
    return ConnectorSettings(secrets_keys=KEY)  # type: ignore[arg-type]


def _adapter(raw: httpx.AsyncClient, token: str = TOKEN) -> JiraAdapter:
    return build_adapter(
        {"base_url": URL, "projects": "HR,ENG,OPEN"},
        {"token": token},
        outbound.OutboundClient(raw),
        _settings(),
    )


def _emails(names: frozenset[str]) -> frozenset[str]:
    return frozenset(email(name) for name in names)


async def test_listing_mirrors_jira_permissions(stand_address: None) -> None:
    async with httpx.AsyncClient() as raw:
        adapter = _adapter(raw)
        await adapter.check()
        documents = [d async for d in adapter.list(["issues", "attachments"])]

    issues = {d.title.split(": ", 1)[1]: d for d in documents if d.module == "issues"}
    # Задачу, которой служебная учётка не видит (уровень безопасности без
    # неё), Jira в поиске не отдаёт — адаптер её не возвращает.
    assert set(issues) == set(EXPECTED_READERS)
    assert not set(HIDDEN_FROM_SERVICE) & set(issues)
    for summary, readers in EXPECTED_READERS.items():
        issue = issues[summary]
        assert issue.visibility is MaterialVisibility.RESTRICTED, summary
        assert issue.allowed_emails == _emails(readers), summary
        assert issue.url.startswith(f"{URL}/browse/"), summary
    assert email(INACTIVE) not in frozenset().union(
        *(d.allowed_emails for d in documents)
    )
    assert issues["Отпуск"].path == "Кадры"

    attachments = {d.title: d for d in documents if d.module == "attachments"}
    assert set(attachments) == {"Заявление.docx"}
    assert attachments["Заявление.docx"].allowed_emails == (
        issues["Отпуск"].allowed_emails
    )
    assert attachments["Заявление.docx"].path.startswith("Кадры/HR-")


async def test_issue_and_attachment_reach_the_extractor(stand_address: None) -> None:
    async with httpx.AsyncClient() as raw:
        adapter = _adapter(raw)
        documents = {
            d.title.split(": ", 1)[-1]: d
            async for d in adapter.list(["issues", "attachments"])
        }
        page = await adapter.fetch(documents["Отпуск"], max_bytes=1024 * 1024)
        file = await adapter.fetch(documents["Заявление.docx"], max_bytes=1024 * 1024)

    assert isinstance(page, FetchedPage)
    markdown = html_to_markdown(page.html)
    assert "Отпуск — **28** календарных дней." in markdown
    assert "Заявление — за две недели." in markdown
    assert "Перенос — по согласованию." in markdown
    # Служебная учётка этот комментарий видит (она администратор), но он
    # ограничен ролью — в материал не идёт.
    assert RESTRICTED_COMMENT not in markdown
    assert isinstance(file, FetchedFile)
    detected = detect_format(file.filename, file.data)
    text = await extract(detected.format, file.data)
    assert "Заявление на отпуск подаётся за две недели." in text


async def test_token_of_plain_user_is_refused(stand_address: None) -> None:
    if not MEMBER_TOKEN:
        pytest.skip("нужен JIRA_DC_MEMBER_TOKEN (токен обычного пользователя)")
    async with httpx.AsyncClient() as raw:
        adapter = _adapter(raw, MEMBER_TOKEN)
        await adapter.check()
        with pytest.raises(AdapterConfigError, match="admin_required"):
            [d async for d in adapter.list(["issues"])]


async def _people(session: AsyncSession) -> dict[str, User]:
    people = {}
    for name in [ADMIN, *USERS]:
        user = make_user(
            email=email(name),
            full_name=name,
            role=UserRole.EMPLOYEE,
            hashed_password=hash_password("x"),
        )
        session.add(user)
        people[name] = user
    await session.commit()
    return people


async def _sync(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    connector: Connector | None,
    raw: httpx.AsyncClient,
) -> tuple[Connector, SyncOutcome]:
    secrets = SecretBox([KEY])
    if connector is None:
        connector = Connector(
            kind=KIND,
            name="Jira DC",
            mode=ConnectorMode.ORGANIZATION.value,
            modules=["issues", "attachments"],
            config={"base_url": URL, "projects": "HR,ENG,OPEN"},
            credentials=secrets.encrypt({"token": TOKEN}),
        )
        session.add(connector)
        await session.commit()
    registry = AdapterRegistry()
    register(registry, _settings())
    service = ConnectorSyncService(
        session_maker, outbound.OutboundClient(raw), registry, secrets, _settings()
    )
    outcome = await service.run(
        connector.tenant_id, connector.id, trigger=SyncTrigger.MANUAL
    )
    assert outcome is not None
    return connector, outcome


async def _readers(
    session: AsyncSession, connector: Connector, people: dict[str, User]
) -> dict[str, frozenset[str]]:
    names = {user.id: name for name, user in people.items()}
    found = {}
    for material in (await materials_of(session, connector)).values():
        if not material.title.startswith(("HR-", "ENG-", "OPEN-")):
            continue
        summary = material.title.split(": ", 1)[1]
        found[summary] = frozenset(
            names[i] for i in await access_of(session, material) if i in names
        )
    return found


async def test_sync_gives_each_employee_only_what_jira_lets_them_read(
    stand_address: None,
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
) -> None:
    people = await _people(session)
    async with httpx.AsyncClient() as raw:
        connector, outcome = await _sync(session, session_maker, None, raw)

    assert outcome.status is SyncRunStatus.SUCCEEDED, outcome.error_code
    assert outcome.stats.added == len(EXPECTED_READERS) + 1
    with tenant_scope(tenant_ctx.id):
        readers = await _readers(session, connector, people)
    assert readers == dict(EXPECTED_READERS)


async def test_access_revoked_in_jira_leaves_kronto_on_next_sync(
    stand_address: None,
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    tenant_ctx: Tenant,
) -> None:
    """bob вышел из engineering, новую задачу удалили: следующий запуск
    убирает доступ и материал без новой версии задачи."""
    if not ADMIN_TOKEN:
        pytest.skip("нужен JIRA_DC_ADMIN_TOKEN: тест меняет состав группы")
    seeder = Seeder(URL, ADMIN_TOKEN)
    created = seeder.http.post(
        "/issue",
        json={
            "fields": {
                "project": {"key": "OPEN"},
                "summary": "Временная",
                "description": "Удалится в тесте.",
                "issuetype": {"name": "Task"},
            }
        },
    )
    created.raise_for_status()
    temporary = created.json()["key"]
    people = await _people(session)
    try:
        async with httpx.AsyncClient() as raw:
            connector, first = await _sync(session, session_maker, None, raw)
            assert first.status is SyncRunStatus.SUCCEEDED
            with tenant_scope(tenant_ctx.id):
                before = await _readers(session, connector, people)
            assert "bob" in before["Архитектура"]
            assert "Временная" in before

            seeder.leave("bob", "engineering")
            seeder.http.delete(f"/issue/{temporary}").raise_for_status()
            temporary = ""
            _, second = await _sync(session, session_maker, connector, raw)

        assert second.status is SyncRunStatus.SUCCEEDED
        assert second.stats.removed == 1
        assert second.stats.updated == 0
        with tenant_scope(tenant_ctx.id):
            after = await _readers(session, connector, people)
        assert "bob" not in after["Архитектура"]
        assert "Временная" not in after
        assert after["Регламент"] == before["Регламент"]
    finally:
        if "bob" not in _members(seeder, "engineering"):
            seeder.join("bob", "engineering")
        if temporary:
            seeder.http.delete(f"/issue/{temporary}")


def _members(seeder: Seeder, group: str) -> set[str]:
    response = seeder.http.get("/group/member", params={"groupname": group})
    response.raise_for_status()
    return {str(u["name"]) for u in response.json().get("values", [])}
