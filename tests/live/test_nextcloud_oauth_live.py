"""Живая проверка вида `nextcloud_oauth`: OAuth2 настоящего Nextcloud —
клиент из `occ oauth2:add-client`, вход сотрудника в браузере
(Playwright, tests/live/nextcloud/oauth.mjs), обмен кода, продление
токенов и синхронизация по ним.

Стенд — тот же, что у test_nextcloud_live.py (tests/live/nextcloud/README.md,
раздел про OAuth2). Без переменных или без браузера — пропуск:

    NEXTCLOUD_URL=https://127.0.0.1:8444/ DAV_STAND_CA=~/dav-tls/cert.pem \\
    NEXTCLOUD_OAUTH_CLIENT_ID=… NEXTCLOUD_OAUTH_CLIENT_SECRET=… \\
        uv run pytest tests/live/test_nextcloud_oauth_live.py -q
"""

import os
import shutil
import subprocess
from glob import glob
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.connectors.base import AdapterAuthError, AdapterConfigError
from corp_ed.connectors.webdav.adapter import build_adapter
from corp_ed.connectors.webdav.oauth import NextcloudOAuth
from corp_ed.core import outbound
from corp_ed.core.security import hash_password
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import Tenant, UserRole
from corp_ed.domain.types import SyncRunStatus
from corp_ed.repositories.connector_repository import GrantRepository
from tests.factories import make_user
from tests.live.dav_stand import CA, PASSWORD, PerUserStand, local_stand, stand_http
from tests.live.nextcloud.seed import EXPECTED
from tests.test_connector_sync import access_of, materials_of

URL = os.environ.get("NEXTCLOUD_URL", "")
CLIENT_ID = os.environ.get("NEXTCLOUD_OAUTH_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("NEXTCLOUD_OAUTH_CLIENT_SECRET", "")
CALLBACK = os.environ.get(
    "NEXTCLOUD_OAUTH_CALLBACK",
    "https://kronto.example.ru/api/v1/connectors/oauth/callback",
)
"""Адрес возврата из регистрации клиента: браузер до него не доходит —
oauth.mjs перехватывает переход и берёт код из адреса."""
KIND = "nextcloud_oauth"
FRONTEND = Path(__file__).resolve().parents[2] / "frontend"

pytestmark = [
    pytest.mark.skipif(
        not (URL and CA and CLIENT_ID and CLIENT_SECRET),
        reason="нужны NEXTCLOUD_URL, DAV_STAND_CA и клиент OAuth2 стенда Nextcloud",
    ),
    pytest.mark.skipif(
        shutil.which("node") is None
        or not (FRONTEND / "node_modules" / "playwright").exists(),
        reason="нужны node и Playwright из frontend/ (npm ci)",
    ),
]


def _oauth(raw: object, secret: str = CLIENT_SECRET) -> NextcloudOAuth:
    return NextcloudOAuth(
        outbound.OutboundClient(raw),  # type: ignore[arg-type]
        server=URL,
        client_id=CLIENT_ID,
        client_secret=secret,
    )


def _login(user: str) -> str:
    """Код авторизации: сотрудник входит в Nextcloud и даёт доступ."""
    authorize = _oauth(None).authorize_url(f"stand-{user}")
    env = dict(os.environ)
    chromium = sorted(glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))
    if chromium and "CHROMIUM_PATH" not in env:
        env["CHROMIUM_PATH"] = chromium[-1]
    node = shutil.which("node") or "node"
    done = subprocess.run(  # noqa: S603 — свой скрипт стенда
        [
            node,
            "../tests/live/nextcloud/oauth.mjs",
            authorize,
            CALLBACK,
            user,
            PASSWORD,
        ],
        cwd=FRONTEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=True,
    )
    return done.stdout.strip().splitlines()[-1]


async def test_login_exchange_and_listing_by_token() -> None:
    code = _login("maria")
    with local_stand(URL):
        async with stand_http() as raw:
            exchanged = await _oauth(raw).exchange(code)
            credentials = {**exchanged.credentials, "client_secret": CLIENT_SECRET}
            adapter = build_adapter(
                KIND,
                {"server": URL, "client_id": CLIENT_ID},
                credentials,
                outbound.OutboundClient(raw),
                PerUserStand(URL).settings,
            )
            await adapter.check()
            documents = {d.title: d async for d in adapter.list(["files"])}

    assert exchanged.external_user_id == "maria"
    assert exchanged.credentials["user_id"] == "maria"
    expected = {title for (title, _), readers in EXPECTED.items() if "maria" in readers}
    assert expected <= set(documents)
    assert documents["Общий.txt"].external_id.startswith(f"{KIND}:id:")
    # Повторный обмен того же кода — отказ: код одноразовый.
    with local_stand(URL):
        async with stand_http() as raw:
            with pytest.raises(AdapterAuthError) as caught:
                await _oauth(raw).exchange(code)
    assert caught.value.code == "invalid_grant"


async def test_refresh_rotates_the_pair_and_the_old_refresh_dies() -> None:
    code = _login("petr")
    with local_stand(URL):
        async with stand_http() as raw:
            oauth = _oauth(raw)
            first = (await oauth.exchange(code)).credentials
            tokens, user_id = await oauth.refresh(first["refresh_token"])
            assert user_id == "petr"
            assert tokens.refresh_token != first["refresh_token"]
            assert tokens.access_token != first["access_token"]
            with pytest.raises(AdapterAuthError) as caught:
                await oauth.refresh(first["refresh_token"])
            assert caught.value.code == "invalid_grant"
            # Просроченный токен адаптер продлевает сам и отдаёт новую пару.
            adapter = build_adapter(
                KIND,
                {"server": URL, "client_id": CLIENT_ID},
                {
                    "access_token": tokens.access_token,
                    "refresh_token": tokens.refresh_token,
                    "expires_at": "1",
                    "user_id": "petr",
                    "client_secret": CLIENT_SECRET,
                },
                outbound.OutboundClient(raw),
                PerUserStand(URL).settings,
            )
            titles = {d.title async for d in adapter.list(["files"])}
    assert "Регламент.pdf" in titles
    renewed = adapter.refreshed_credentials
    assert renewed is not None
    assert renewed["refresh_token"] not in (
        tokens.refresh_token,
        first["refresh_token"],
    )


async def test_wrong_client_secret_is_a_connector_error() -> None:
    code = _login("ivan")
    with local_stand(URL):
        async with stand_http() as raw:
            with pytest.raises(AdapterConfigError) as caught:
                await _oauth(raw, secret="not-the-secret").exchange(code)
    assert caught.value.code == "invalid_client"


async def test_sync_by_tokens_keeps_one_document_per_shared_file(
    session: AsyncSession,
    tenant_ctx: Tenant,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    people = {}
    for login in ("ivan", "maria"):
        user = make_user(
            email=f"{login}@example.com",
            full_name=login,
            role=UserRole.EMPLOYEE,
            hashed_password=hash_password("Password-1234"),
        )
        session.add(user)
        people[login] = user
    await session.commit()
    stand = PerUserStand(URL)
    connector = await stand.connector(
        session,
        KIND,
        {"server": URL, "client_id": CLIENT_ID},
        {"client_secret": CLIENT_SECRET},
    )
    grants = {}
    with local_stand(URL):
        async with stand_http() as raw:
            for login in people:
                exchanged = await _oauth(raw).exchange(_login(login))
                # Токен «истёк»: синхронизация продлит его и сохранит пару.
                credentials = {**exchanged.credentials, "expires_at": "1"}
                grants[login] = await stand.grant(
                    session, connector, people[login].id, credentials
                )

    outcome = await stand.sync(session_maker, connector)

    assert outcome.status is SyncRunStatus.SUCCEEDED, outcome
    assert outcome.stats.grants == 2 and outcome.stats.grants_expired == 0
    names = {user.id: login for login, user in people.items()}
    with tenant_scope(tenant_ctx.id):
        materials = list((await materials_of(session, connector)).values())
        for (title, text), readers in EXPECTED.items():
            found = [m for m in materials if m.title == title and text in m.content]
            visible = readers & set(people)
            assert len(found) == (1 if visible else 0), title
            if found:
                who = {names[i] for i in await access_of(session, found[0])}
                assert who == visible, title
        for login, grant in grants.items():
            stored = await GrantRepository(session).credentials_of(grant.id)
            assert stored is not None
            saved = stand.secrets.decrypt(stored)
            assert int(saved["expires_at"]) > 1, login
            assert saved["user_id"] == login
