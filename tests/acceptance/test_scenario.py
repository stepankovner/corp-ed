"""Сценарий приёмки целиком — на поддельной системе (без Docker).

Проверяет и сам сценарий: он обязан заметить, что закрытый документ
отдан всем и что отзыв доступа не дошёл до ядра.
"""

from collections.abc import Iterable

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.acceptance.fake import FakeDriver, fake_system
from tests.acceptance.model import Manifest, load_manifest
from tests.acceptance.scenario import (
    FAIL,
    OK,
    StepResult,
    default_registry_for,
    new_key,
    run_system,
)
from tests.fake_connector import FakeSource, make_registry, plain_extractor


@pytest.fixture(scope="module")
def manifest() -> Manifest:
    return load_manifest()


async def run_fake(
    manifest: Manifest,
    session_maker: async_sessionmaker[AsyncSession],
    *,
    leak_zones: Iterable[str] = (),
    ignore_revoke: bool = False,
) -> list[StepResult]:
    system = fake_system(manifest)
    source = FakeSource()
    driver = FakeDriver(
        system, manifest, source, leak_zones=leak_zones, ignore_revoke=ignore_revoke
    )
    from tests.acceptance.model import Expected

    driver.seed(Expected(manifest, system))
    return await run_system(
        manifest,
        system,
        driver,
        session_maker,
        make_registry(source),
        ca="",
        extractor=plain_extractor,
    )


async def test_every_step_passes_on_a_faithful_source(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    manifest: Manifest,
) -> None:
    results = await run_fake(manifest, session_maker)

    assert [r.step for r in results] == [
        "sync",
        "resync",
        "edit",
        "delete",
        "revoke",
        "move",
    ]
    for result in results:
        assert result.status == OK, (result.step, result.details)
    assert results[0].stats["added"] == len(manifest.systems["bookstack"].docs)
    assert results[1].stats["added"] == 0
    assert results[2].stats["applied"]["edit"] == 3
    assert results[3].stats["removed"] == 2


async def test_leak_of_a_closed_document_is_caught(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    manifest: Manifest,
) -> None:
    results = await run_fake(manifest, session_maker, leak_zones=["payroll"])

    first = results[0]
    assert first.status == FAIL
    leaks = [d for d in first.details if d.startswith("УТЕЧКА")]
    assert any("kuznetsova видит SV-PAY-001" in d for d in leaks)
    # Шаги изменений продолжаются и тоже видят утечку, а не «ok».
    assert all(r.status == FAIL for r in results if r.step == "sync")


async def test_revoke_that_never_reached_the_source_is_caught(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    manifest: Manifest,
) -> None:
    results = {
        r.step: r for r in await run_fake(manifest, session_maker, ignore_revoke=True)
    }

    assert results["sync"].status == OK
    revoke = results["revoke"]
    assert revoke.status == FAIL
    assert any("УТЕЧКА: petrenko видит SV-PAY-001" in d for d in revoke.details)
    assert any("УТЕЧКА: lebedeva видит SV-SAL-001" in d for d in revoke.details)


async def test_system_without_credentials_is_skipped(
    session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    manifest: Manifest,
) -> None:
    from tests.acceptance.systems import make_driver

    system = manifest.systems["kaiten"]
    results = await run_system(
        manifest,
        system,
        make_driver(system, manifest, None),
        session_maker,
        default_registry_for(new_key()),
        ca="",
    )

    assert {r.status for r in results} == {"skip"}
    assert "нет учётных данных" in results[0].details[0]
