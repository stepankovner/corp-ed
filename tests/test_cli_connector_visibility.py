"""cli connector-visibility: кто какие документы подключения видит."""

import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed import cli
from corp_ed.domain.models import (
    Connector,
    Material,
    MaterialAccess,
    MemberStatus,
    Tenant,
    User,
)
from corp_ed.services.visibility_report import DEFAULT_CODE_PATTERN
from tests.acceptance.scenario import CODE
from tests.factories import make_user


def doc(code: str, body: str = "текст") -> str:
    return f"Шифр документа: {code}\n\n{body}"


async def _member(
    session: AsyncSession,
    tenant: Tenant,
    email: str,
    status: MemberStatus = MemberStatus.ACTIVE,
) -> User:
    user = make_user(id=uuid4(), tenant_id=tenant.id, email=email, status=status)
    session.add(user)
    await session.commit()
    return user


async def _connector(session: AsyncSession, tenant: Tenant, name: str) -> Connector:
    connector = Connector(
        tenant_id=tenant.id,
        kind="yandex360",
        name=name,
        mode="organization",
        modules=["disk"],
        config={},
        credentials="шифротекст-не-должен-попасть-в-отчёт",
    )
    session.add(connector)
    await session.commit()
    return connector


def _material(
    tenant: Tenant,
    connector: Connector | None,
    content: str,
    external_id: str,
    visibility: str = "tenant",
) -> Material:
    return Material(
        tenant_id=tenant.id,
        connector_id=connector.id if connector else None,
        external_id=external_id if connector else None,
        title=external_id,
        content=content,
        visibility=visibility,
    )


@pytest.fixture(autouse=True)
def stand_session_maker(
    monkeypatch: pytest.MonkeyPatch,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setattr(cli, "get_session_maker", lambda: session_maker)


async def run_cli(*argv: str) -> int:
    return await cli._run(cli._parser().parse_args(["connector-visibility", *argv]))


async def test_report_shows_who_sees_which_codes(
    session: AsyncSession,
    tenant_ctx: Tenant,
    capsys: pytest.CaptureFixture[str],
) -> None:
    belova = await _member(session, tenant_ctx, "belova@example.test")
    vetrov = await _member(session, tenant_ctx, "vetrov@example.test")
    await _member(session, tenant_ctx, "left@example.test", MemberStatus.BLOCKED)
    connector = await _connector(session, tenant_ctx, "Облако")
    other = await _connector(session, tenant_ctx, "Другое")

    open_doc = _material(tenant_ctx, connector, doc("SV-GEN-001"), "a")
    closed = _material(tenant_ctx, connector, doc("SV-HR-001"), "b", "restricted")
    nobody = _material(tenant_ctx, connector, doc("SV-PAY-001"), "c", "restricted")
    # у per_user один документ может лежать копиями — по копии на сотрудника
    copy_1 = _material(tenant_ctx, connector, doc("SV-GEN-002"), "d1")
    copy_2 = _material(tenant_ctx, connector, doc("SV-GEN-002"), "d2")
    sample = _material(tenant_ctx, connector, "Файл-образец без шифра", "e")
    elsewhere = _material(tenant_ctx, other, doc("SV-IT-001"), "f")
    manual = _material(tenant_ctx, None, doc("SV-MGT-001"), "g")
    session.add_all(
        [open_doc, closed, nobody, copy_1, copy_2, sample, elsewhere, manual]
    )
    await session.flush()
    session.add(MaterialAccess(material_id=closed.id, user_id=belova.id))
    await session.commit()

    code = await run_cli("--code", "test", "--connector", str(connector.id))

    out = capsys.readouterr().out
    assert code == 0, out
    assert "шифротекст" not in out
    report = json.loads(out)
    assert report["company"] == "test"
    [one] = report["connectors"]
    assert one == {
        "id": str(connector.id),
        "kind": "yandex360",
        "name": "Облако",
        "materials": 6,
        "documents": {
            "SV-GEN-001": 1,
            "SV-GEN-002": 2,
            "SV-HR-001": 1,
            "SV-PAY-001": 1,
        },
        "without_code": 1,
        "seen": {
            "belova@example.test": ["SV-GEN-001", "SV-GEN-002", "SV-HR-001"],
            "vetrov@example.test": ["SV-GEN-001", "SV-GEN-002"],
        },
    }
    # заблокированный сотрудник в отчёт не входит, а закрытый документ
    # не виден тому, кого в доступе нет
    assert "SV-HR-001" not in one["seen"][vetrov.email or ""]
    assert "left@example.test" not in one["seen"]
    assert out.index('"belova') < out.index('"vetrov')


async def test_all_connectors_by_default_and_unknown_ones_fail(
    session: AsyncSession,
    tenant_ctx: Tenant,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await _member(session, tenant_ctx, "belova@example.test")
    first = await _connector(session, tenant_ctx, "Первое")
    second = await _connector(session, tenant_ctx, "Второе")
    session.add(_material(tenant_ctx, second, doc("SV-IT-001"), "x"))
    await session.commit()

    assert await run_cli("--code", "test") == 0
    report = json.loads(capsys.readouterr().out)
    by_id = {c["id"]: c for c in report["connectors"]}
    assert set(by_id) == {str(first.id), str(second.id)}
    assert by_id[str(first.id)]["materials"] == 0
    assert by_id[str(first.id)]["seen"] == {"belova@example.test": []}
    assert by_id[str(second.id)]["seen"] == {"belova@example.test": ["SV-IT-001"]}

    assert await run_cli("--code", "test", "--connector", str(UUID(int=1))) == 1
    assert "нет такого подключения" in capsys.readouterr().err
    assert await run_cli("--code", "nope") == 1
    assert "компании с кодом nope нет" in capsys.readouterr().err


async def test_custom_code_pattern(
    session: AsyncSession,
    tenant_ctx: Tenant,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await _member(session, tenant_ctx, "belova@example.test")
    connector = await _connector(session, tenant_ctx, "Облако")
    session.add(_material(tenant_ctx, connector, "Код: DOC-7 и текст", "a"))
    await session.commit()

    assert await run_cli("--code", "test", "--code-pattern", r"Код: (DOC-\d+)") == 0
    [one] = json.loads(capsys.readouterr().out)["connectors"]
    assert one["documents"] == {"DOC-7": 1}
    assert one["seen"] == {"belova@example.test": ["DOC-7"]}

    assert await run_cli("--code", "test", "--code-pattern", "без группы") == 1
    assert "нужна группа" in capsys.readouterr().err
    assert await run_cli("--code", "test", "--code-pattern", "(") == 1
    assert "неверный шаблон" in capsys.readouterr().err


def test_default_pattern_matches_acceptance() -> None:
    assert CODE.pattern == DEFAULT_CODE_PATTERN
