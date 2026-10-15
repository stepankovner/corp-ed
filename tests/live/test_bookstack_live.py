"""Живая проверка вида `bookstack` (режим organization): образ
linuxserver/bookstack в Docker, API-токен администратора, роли с правами
содержимого на книге, главе и странице, «своё», явный запрет.

Как поднять стенд — tests/live/bookstack/README.md (compose, seed.py —
он же выпускает токены). Без переменных — пропуск:

    BOOKSTACK_URL=https://127.0.0.1:8460/ BOOKSTACK_TOKENS=~/bookstack-stand.json \\
    DAV_STAND_CA=~/dav-tls/cert.pem \\
        uv run pytest tests/live/test_bookstack_live.py -q --no-cov

Главная проверка — сверка с самим BookStack: что адаптер считает
видимым каждому, BookStack отдаёт этому пользователю по его токену
(`/api/pages`, `/api/attachments`).
"""

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.base import (
    AdapterAuthError,
    FetchedFile,
    FetchedPage,
    RemoteDocument,
)
from corp_ed.connectors.bookstack import register
from corp_ed.connectors.bookstack.adapter import SPEC, BookStackAdapter, build_adapter
from corp_ed.connectors.registry import AdapterRegistry, default_registry
from corp_ed.core import outbound
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.secrets import SecretBox
from corp_ed.domain.models import Connector, Tenant, User
from corp_ed.domain.types import (
    ConnectorMode,
    MaterialVisibility,
    SyncRunStatus,
    SyncTrigger,
)
from corp_ed.services.connector_sync_service import ConnectorSyncService, SyncOutcome
from tests.live.bookstack.seed import (
    ATTACHMENTS,
    EVERYONE,
    EXPECTED,
    PATHS,
    TEXT,
    USERS,
    BookStackApi,
    email,
)
from tests.live.dav_stand import CA, _check, local_stand, stand_http, trust
from tests.live.oc_family import make_people, materials, readers

URL = os.environ.get("BOOKSTACK_URL", "")
TOKENS_FILE = os.environ.get("BOOKSTACK_TOKENS", "")
KEY = Fernet.generate_key().decode()

pytestmark = pytest.mark.skipif(
    not (URL and TOKENS_FILE and CA),
    reason="нужны BOOKSTACK_URL, BOOKSTACK_TOKENS и DAV_STAND_CA (стенд BookStack)",
)


def _tokens() -> dict[str, tuple[str, str]]:
    data = json.loads(Path(TOKENS_FILE).expanduser().read_text(encoding="utf-8"))
    return {k: (v["id"], v["secret"]) for k, v in data["tokens"].items()}


def _settings() -> ConnectorSettings:
    return ConnectorSettings(secrets_keys=KEY)  # type: ignore[arg-type]


def _registry() -> AdapterRegistry:
    registry = AdapterRegistry()
    register(registry, _settings())
    return registry


def _adapter(raw: object, token: tuple[str, str] | None = None) -> BookStackAdapter:
    token_id, secret = token or _tokens()["admin"]
    return build_adapter(
        SPEC,
        {"base_url": URL},
        {"token_id": token_id, "token_secret": secret},
        outbound.OutboundClient(raw),  # type: ignore[arg-type]
        _settings(),
    )


async def _listing() -> dict[str, RemoteDocument]:
    with local_stand(URL):
        async with stand_http() as raw:
            adapter = _adapter(raw)
            await adapter.check()
            return {d.title: d async for d in adapter.list(["pages", "attachments"])}


def _sees(document: RemoteDocument, login: str) -> bool:
    if document.visibility is MaterialVisibility.TENANT:
        return True
    return email(login) in document.allowed_emails


@pytest.fixture
def api() -> Iterator[BookStackApi]:
    client = BookStackApi(URL, *_tokens()["admin"])
    yield client
    client.close()


async def test_listing_mirrors_bookstack_permissions() -> None:
    documents = await _listing()
    for title, allowed in EXPECTED.items():
        document = documents[title]
        if allowed is None:
            assert document.visibility is MaterialVisibility.TENANT, title
            assert document.allowed_emails == frozenset(), title
        else:
            assert document.visibility is MaterialVisibility.RESTRICTED, title
            assert document.allowed_emails == {email(u) for u in allowed}, title
        assert document.path == PATHS[title], title
        assert document.url.startswith(f"{URL}books/"), title
    files = {d.title for d in documents.values() if d.module == "attachments"}
    # Вложение-ссылка не индексируется.
    assert files == set(ATTACHMENTS)


async def test_rights_match_what_bookstack_shows_each_user() -> None:
    """Сверка с BookStack: страницы и вложения, которые пользователь
    получает по своему токену, — ровно те, что адаптер отдал ему."""
    documents = await _listing()
    pages = {d.title: d for d in documents.values() if d.module == "pages"}
    files = {d.title: d for d in documents.values() if d.module == "attachments"}
    tokens = _tokens()
    assert set(tokens) == EVERYONE
    with httpx.Client(verify=trust(CA), timeout=60) as http:
        for login, (token_id, secret) in tokens.items():
            headers = {"Authorization": f"Token {token_id}:{secret}"}
            seen = {
                p["name"]
                for p in http.get(
                    f"{URL}api/pages", params={"count": 500}, headers=headers
                ).json()["data"]
            }
            assert seen == {t for t, d in pages.items() if _sees(d, login)}, login
            attached = {
                a["name"]
                for a in http.get(
                    f"{URL}api/attachments", params={"count": 500}, headers=headers
                ).json()["data"]
                if not a["external"]
            }
            assert attached == {t for t, d in files.items() if _sees(d, login)}, login


async def test_pages_and_files_are_fetched() -> None:
    with local_stand(URL):
        async with stand_http() as raw:
            adapter = _adapter(raw)
            documents = {
                d.title: d async for d in adapter.list(["pages", "attachments"])
            }
            fetched = {
                title: await adapter.fetch(document, max_bytes=1_000_000)
                for title, document in documents.items()
            }
    for title, text in TEXT.items():
        content = fetched[title]
        assert isinstance(content, FetchedPage), title
        assert content.html.startswith(f"<h1>{title}</h1>"), title
        assert text in content.html, title
    for filename, (_, text) in ATTACHMENTS.items():
        content = fetched[filename]
        assert isinstance(content, FetchedFile), filename
        assert content.data == text.encode()
        assert content.filename == filename


async def test_wrong_token_is_an_auth_error() -> None:
    with local_stand(URL):
        async with stand_http() as raw:
            with pytest.raises(AdapterAuthError) as caught:
                await _adapter(raw, ("not-a-token", "not-a-secret")).check()
    assert caught.value.code == "unauthorized"


async def test_employee_token_is_refused() -> None:
    """Токен сотрудника (есть «Access System API», нет управления
    пользователями и ролями) — без почт и ролей права не посчитать."""
    with local_stand(URL):
        async with stand_http() as raw:
            with pytest.raises(AdapterAuthError) as caught:
                await _adapter(raw, _tokens()["petr"]).check()
    assert caught.value.code == "admin_required"


# --- синхронизация ------------------------------------------------------------


@pytest.fixture
async def people(session: AsyncSession, tenant_ctx: Tenant) -> dict[str, User]:
    """Сотрудники kronto с той же почтой; admin в kronto нет."""
    return await make_people(session, USERS)


async def _connector(session: AsyncSession) -> Connector:
    token_id, secret = _tokens()["admin"]
    connector = Connector(
        kind="bookstack",
        name="BookStack (стенд)",
        mode=ConnectorMode.ORGANIZATION.value,
        modules=["pages", "attachments"],
        config={"base_url": URL},
        credentials=SecretBox([KEY]).encrypt(
            {"token_id": token_id, "token_secret": secret}
        ),
    )
    session.add(connector)
    await session.commit()
    return connector


async def _sync(
    session_maker: async_sessionmaker[AsyncSession], connector: Connector
) -> SyncOutcome:
    with local_stand(URL):
        async with stand_http() as raw:
            service = ConnectorSyncService(
                session_maker,
                outbound.OutboundClient(raw),
                _registry(),
                SecretBox([KEY]),
                _settings(),
            )
            outcome = await service.run(
                connector.tenant_id, connector.id, trigger=SyncTrigger.MANUAL
            )
    assert outcome is not None
    assert outcome.status is SyncRunStatus.SUCCEEDED, outcome
    assert outcome.stats.failed == 0, outcome
    return outcome


def _restore(api: BookStackApi, page_id: int) -> None:
    for deletion in api.listing("recycle-bin"):
        if deletion["deletable_type"] == "page" and deletion["deletable_id"] == page_id:
            api.call("PUT", f"recycle-bin/{deletion['id']}")


async def test_sync_mirrors_access_and_follows_changes(
    session: AsyncSession,
    tenant_ctx: Tenant,
    session_maker: async_sessionmaker[AsyncSession],
    people: dict[str, User],
    api: BookStackApi,
) -> None:
    connector = await _connector(session)

    await _sync(session_maker, connector)

    by_title = {m.title: m for m in await materials(session, connector)}
    for title, allowed in EXPECTED.items():
        material = by_title[title]
        assert TEXT[title] in material.content, title
        if allowed is None:
            assert material.visibility == MaterialVisibility.TENANT.value, title
            continue
        assert material.visibility == MaterialVisibility.RESTRICTED.value, title
        assert await readers(session, material, people) == allowed - {"admin"}, title
    assert await readers(session, by_title["ведомость.txt"], people) == {"maria"}

    # Правка текста, удаление страницы в корзину и выход maria из «Кадров»
    # — права меняются без новой версии документа.
    users = api.users()
    roles = api.roles()
    plan = int(api.find("pages", "План")["id"])
    estimate = int(api.find("pages", "Смета")["id"])
    api.call("PUT", f"pages/{plan}", json={"html": "<p>Запуск — в июне, версия 2.</p>"})
    api.call("DELETE", f"pages/{estimate}")
    api.call("PUT", f"users/{users['maria']}", json={"roles": [roles["Сотрудники"]]})
    try:
        outcome = await _sync(session_maker, connector)

        assert outcome.stats.updated == 1
        assert outcome.stats.removed == 1
        by_title = {m.title: m for m in await materials(session, connector)}
        assert "версия 2" in by_title["План"].content
        assert "Смета" not in by_title
        assert await readers(session, by_title["Зарплаты"], people) == set()
        assert await readers(session, by_title["ведомость.txt"], people) == set()
    finally:
        api.call("PUT", f"pages/{plan}", json={"html": f"<p>{TEXT['План']}</p>"})
        _restore(api, estimate)
        api.call(
            "PUT",
            f"users/{users['maria']}",
            json={"roles": [roles["Сотрудники"], roles["Кадры"]]},
        )


async def test_connector_check_records_without_the_token(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`cli connector-check` берёт default_registry, куда вид подключит
    интегратор; здесь — тот же реестр плюс bookstack."""
    import corp_ed.cli as cli

    def with_bookstack(settings: Any) -> AdapterRegistry:
        registry = default_registry(settings)
        register(registry, settings)
        return registry

    monkeypatch.setattr(cli, "default_registry", with_bookstack)
    token_id, secret = _tokens()["admin"]
    record = tmp_path / "bookstack"
    code = await _check(
        URL,
        CA,
        [
            "--kind",
            "bookstack",
            "--config",
            f"base_url={URL}",
            "--credential",
            f"token_id={token_id}",
            "--credential",
            f"token_secret={secret}",
            "--fetch",
            "2",
            "--record",
            str(record),
        ],
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "check: ok" in out
    dumped = "\n".join(f.read_text(encoding="utf-8") for f in record.glob("*.json"))
    assert "content-permissions" in dumped or "role_permissions" in dumped
    assert token_id not in dumped and secret not in dumped
