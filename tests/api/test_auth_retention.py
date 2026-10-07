"""Срок хранения записей входа: refresh-токены, шаги входа, ссылки из
писем, доверенные устройства (cli purge).

Истёкшие записи удаляются, но не сразу: refresh-токены живут ещё
REFRESH_TOKEN_GRACE после срока, иначе повтор украденного токена не
отозвал бы цепочку, а «Выйти» перестал бы закрывать access-токен.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import InstrumentedAttribute

from corp_ed.api.v1.session_cookie import REFRESH_COOKIE
from corp_ed.core import security
from corp_ed.domain.models import (
    AuthChallenge,
    EmailToken,
    RefreshToken,
    TrustedDevice,
    User,
)
from corp_ed.services.retention_service import (
    AUTH_RECORD_GRACE,
    REFRESH_TOKEN_GRACE,
    RetentionService,
)
from tests.api.conftest import login, refresh_token_of, refresh_with


def _refresh(
    account_id: UUID,
    family_id: UUID,
    *,
    created_at: datetime,
    expires_at: datetime,
    used: bool = False,
) -> RefreshToken:
    return RefreshToken(
        account_id=account_id,
        family_id=family_id,
        token_hash=uuid4().hex + uuid4().hex,
        created_at=created_at,
        expires_at=expires_at,
        used_at=created_at if used else None,
    )


async def _ids(session: AsyncSession, column: InstrumentedAttribute[UUID]) -> set[UUID]:
    return set((await session.scalars(select(column))).all())


async def test_purge_removes_sign_in_records_only_after_grace(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    account: User,
) -> None:
    now = datetime.now(UTC)
    owner = account.account_id
    past_grace = now - REFRESH_TOKEN_GRACE - timedelta(days=1)
    within_grace = now - REFRESH_TOKEN_GRACE + timedelta(days=1)

    # Закончившийся вход: истёк дольше REFRESH_TOKEN_GRACE назад — удаляется.
    dead = _refresh(
        owner,
        uuid4(),
        created_at=past_grace - timedelta(days=30),
        expires_at=past_grace,
        used=True,
    )
    # Истёк недавно: повтор ещё должен отзывать цепочку — остаётся.
    recent = _refresh(
        owner,
        uuid4(),
        created_at=within_grace - timedelta(days=30),
        expires_at=within_grace,
        used=True,
    )
    # Живой сеанс, которым пользуются давно: старые звенья цепочки
    # удаляются, кроме первого — по нему список сеансов показывает, когда
    # вход начался.
    family = uuid4()
    first = _refresh(
        owner,
        family,
        created_at=now - timedelta(days=200),
        expires_at=now - timedelta(days=170),
        used=True,
    )
    middle = _refresh(
        owner,
        family,
        created_at=now - timedelta(days=150),
        expires_at=now - timedelta(days=120),
        used=True,
    )
    live = _refresh(owner, family, created_at=now, expires_at=now + timedelta(days=30))
    session.add_all([dead, recent, first, middle, live])

    long_expired = now - AUTH_RECORD_GRACE - timedelta(hours=1)
    just_expired = now - timedelta(minutes=5)
    challenges = [
        AuthChallenge(
            account_id=owner,
            purpose="login",
            token_hash=uuid4().hex,
            expires_at=expires_at,
        )
        for expires_at in (long_expired, just_expired, now + timedelta(minutes=10))
    ]
    email_tokens = [
        EmailToken(
            account_id=owner,
            purpose="reset_password",
            token_hash=uuid4().hex,
            expires_at=expires_at,
            used_at=now - timedelta(days=10),
        )
        for expires_at in (long_expired, just_expired, now + timedelta(days=7))
    ]
    devices = [
        TrustedDevice(account_id=owner, token_hash=uuid4().hex, expires_at=expires_at)
        for expires_at in (long_expired, just_expired, now + timedelta(days=30))
    ]
    session.add_all([*challenges, *email_tokens, *devices])
    await session.commit()

    report = await RetentionService(session_maker, qa_log_days=365).purge()

    assert report.refresh_tokens == 2
    assert await _ids(session, RefreshToken.id) == {recent.id, first.id, live.id}
    assert (report.auth_challenges, report.email_tokens, report.trusted_devices) == (
        1,
        1,
        1,
    )
    assert await _ids(session, AuthChallenge.id) == {c.id for c in challenges[1:]}
    assert await _ids(session, EmailToken.id) == {t.id for t in email_tokens[1:]}
    assert await _ids(session, TrustedDevice.id) == {d.id for d in devices[1:]}


async def test_purge_removes_whole_family_once_every_link_is_past_grace(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    account: User,
) -> None:
    now = datetime.now(UTC)
    family = uuid4()
    session.add_all(
        [
            _refresh(
                account.account_id,
                family,
                created_at=now - timedelta(days=200 - step),
                expires_at=now - timedelta(days=170 - step),
                used=step < 2,
            )
            for step in range(3)
        ]
    )
    await session.commit()

    report = await RetentionService(session_maker, qa_log_days=365).purge()

    assert report.refresh_tokens == 3
    assert await _ids(session, RefreshToken.id) == set()


async def test_reuse_of_rotated_token_still_revokes_family_after_purge(
    api: httpx.AsyncClient,
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    account: User,
) -> None:
    """Украденный токен уже истёк, но повтор всё равно закрывает сеанс:
    запись использованного токена переживает срок на REFRESH_TOKEN_GRACE.

    Украден не первый токен цепочки: первую запись purge бережёт и без
    запаса (список сеансов), проверяется именно запас."""
    first = refresh_token_of(await login(api, account.email))
    stolen = refresh_token_of(await refresh_with(api, first))
    rotated = refresh_token_of(await refresh_with(api, stolen))

    record = (
        await session.scalars(
            select(RefreshToken).where(
                RefreshToken.token_hash == security.hash_refresh_token(stolen)
            )
        )
    ).one()
    assert record.used_at is not None
    record.expires_at = datetime.now(UTC) - timedelta(days=1)
    await session.commit()

    await RetentionService(session_maker, qa_log_days=365).purge()
    assert record.id in await _ids(session, RefreshToken.id)

    replay = await refresh_with(api, stolen)
    assert replay.status_code == 401
    after = await refresh_with(api, rotated)
    assert after.status_code == 401


async def test_closed_session_access_token_stays_rejected_after_purge(
    api: httpx.AsyncClient,
    session_maker: async_sessionmaker[AsyncSession],
    account: User,
) -> None:
    signed_in = await login(api, account.email, remember=False)
    access = {"Authorization": f"Bearer {signed_in.json()['access_token']}"}
    logout = await api.post(
        "/api/v1/auth/logout",
        headers={**access, "Cookie": f"{REFRESH_COOKIE}={refresh_token_of(signed_in)}"},
    )
    assert logout.status_code == 204

    await RetentionService(session_maker, qa_log_days=365).purge()

    assert (await api.get("/api/v1/auth/me", headers=access)).status_code == 401
