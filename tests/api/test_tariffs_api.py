"""Тарифы компании (решение Артёма 30.09, domain/tariffs.py).

«Базовый» — до 5 разных рабочих систем (подключений к одной системе —
сколько угодно), «Расширенный» — без тарифного лимита, «Корпоративный» —
ещё и системы вне базового списка. Технический потолок подключений
действует в любом тарифе.
"""

from collections.abc import AsyncGenerator
from dataclasses import replace
from typing import Annotated

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.api.v1.dependencies import (
    get_audit_repository,
    get_connector_service,
)
from corp_ed.cli import _parser
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.database import get_session
from corp_ed.core.outbound import OutboundClient
from corp_ed.core.secrets import SecretBox
from corp_ed.domain.models import AuditEvent, Tenant, User
from corp_ed.domain.tariffs import (
    BASE_MAX_SYSTEMS,
    PLANS,
    Tariff,
    plan_for,
    system_fits,
)
from corp_ed.main import app
from corp_ed.repositories.audit_repository import AuditRepository
from corp_ed.repositories.connector_repository import (
    ConnectorRepository,
    GrantRepository,
    SyncRunRepository,
)
from corp_ed.repositories.connector_sync_job_repository import (
    ConnectorSyncJobRepository,
)
from corp_ed.repositories.material_repository import MaterialRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services.connector_service import ConnectorService
from corp_ed.services.tenant_service import TenantService
from tests.api.conftest import bearer
from tests.fake_connector import ORG_SPEC, FakeSource, make_registry, public_resolver

URL = "/api/v1/connectors"
CREATE = {
    "kind": ORG_SPEC.kind,
    "name": "Портал",
    "modules": ["docs"],
    "config": {"base_url": "https://portal.example.com/rest/"},
}
NON_BASE = replace(ORG_SPEC, kind="custom_erp", title="Система клиента", base=False)
NON_BASE_CREATE = {**CREATE, "kind": NON_BASE.kind}
# Ещё пять базовых систем: «Базовый» считает разные системы, а не подключения.
OTHER_SYSTEMS = [
    replace(ORG_SPEC, kind=f"system_{n}", title=f"Система {n}") for n in range(2, 7)
]


def _system(n: int) -> dict[str, object]:
    """Подключение к n-й системе: 1 — ORG_SPEC, 2…6 — OTHER_SYSTEMS."""
    return CREATE if n == 1 else {**CREATE, "kind": f"system_{n}"}


@pytest.fixture
async def tariff_api(
    api: httpx.AsyncClient,
) -> AsyncGenerator[httpx.AsyncClient]:
    """Реестр с базовой и небазовой системой; технический потолок 7 —
    выше лимита «Базового», чтобы видеть, какое ограничение сработало."""
    registry = make_registry(FakeSource())
    registry.register(NON_BASE, lambda *args: None)  # type: ignore[arg-type,return-value]
    for spec in OTHER_SYSTEMS:
        registry.register(spec, lambda *args: None)  # type: ignore[arg-type,return-value]
    settings = ConnectorSettings(
        secrets_keys=Fernet.generate_key().decode(),  # type: ignore[arg-type]
        max_per_tenant=7,
    )

    def dependency(
        session: Annotated[AsyncSession, Depends(get_session)],
        audit: Annotated[AuditRepository, Depends(get_audit_repository)],
    ) -> ConnectorService:
        return ConnectorService(
            ConnectorRepository(session),
            GrantRepository(session),
            SyncRunRepository(session),
            ConnectorSyncJobRepository(session),
            MaterialRepository(session),
            audit,
            SecretBox(settings.keys),
            registry,
            settings,
            session,
            OutboundClient(
                httpx.AsyncClient(
                    transport=httpx.MockTransport(lambda r: httpx.Response(500))
                )
            ),
            resolver=public_resolver,
        )

    app.dependency_overrides[get_connector_service] = dependency
    yield api
    app.dependency_overrides.pop(get_connector_service, None)


async def _tariff(session: AsyncSession, tenant: Tenant, tariff: Tariff) -> None:
    tenant.tariff = tariff.value
    await session.commit()


async def _create_many(
    api: httpx.AsyncClient, user: User, count: int, body: dict[str, object]
) -> list[int]:
    return [
        (await api.post(URL, json=body, headers=bearer(user))).status_code
        for _ in range(count)
    ]


# --- чистые правила ---------------------------------------------------------------


def test_plans() -> None:
    assert [plan.title for plan in PLANS.values()] == [
        "Базовый",
        "Расширенный",
        "Корпоративный",
    ]
    assert plan_for("base").max_systems == BASE_MAX_SYSTEMS == 5
    assert plan_for(Tariff.EXTENDED).max_systems is None
    assert not plan_for("extended").non_base_connectors
    assert plan_for("enterprise").non_base_connectors


FIVE = frozenset({"a", "b", "c", "d", "e"})


@pytest.mark.parametrize(
    ("tariff", "used", "kind", "fits"),
    [
        ("base", frozenset({"a", "b", "c", "d"}), "e", True),
        ("base", FIVE, "f", False),
        # Ещё одно подключение к уже подключённой системе — не новая система.
        ("base", FIVE, "a", True),
        ("extended", FIVE, "f", True),
        ("enterprise", FIVE, "f", True),
    ],
)
def test_system_fits_counts_distinct_systems(
    tariff: str, used: frozenset[str], kind: str, fits: bool
) -> None:
    assert system_fits(plan_for(tariff), used, kind) is fits


# --- API ------------------------------------------------------------------------


async def test_base_tariff_allows_five_systems(
    tariff_api: httpx.AsyncClient, admin_account: User
) -> None:
    for n in range(1, 6):
        response = await tariff_api.post(
            URL, json=_system(n), headers=bearer(admin_account)
        )
        assert response.status_code == 201, response.text

    response = await tariff_api.post(
        URL, json=_system(6), headers=bearer(admin_account)
    )

    assert response.status_code == 409
    assert response.json()["code"] == "tariff_connector_limit"
    assert "«Базовый» — до 5 рабочих систем" in response.json()["detail"]
    assert "«Расширенный»" in response.json()["detail"]
    # Шестая система не влезла, а второе подключение к первой — можно.
    again = await tariff_api.post(URL, json=_system(1), headers=bearer(admin_account))
    assert again.status_code == 201


async def test_base_tariff_counts_connections_to_one_system_once(
    tariff_api: httpx.AsyncClient, admin_account: User
) -> None:
    """Подключений к одной системе — сколько угодно, до технического
    потолка (здесь 7)."""
    assert await _create_many(tariff_api, admin_account, 7, CREATE) == [201] * 7

    response = await tariff_api.post(URL, json=CREATE, headers=bearer(admin_account))

    assert response.status_code == 409
    assert response.json()["code"] == "connector_limit"


async def test_extended_tariff_stops_only_at_the_technical_cap(
    tariff_api: httpx.AsyncClient,
    admin_account: User,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    await _tariff(session, tenant_ctx, Tariff.EXTENDED)

    assert await _create_many(tariff_api, admin_account, 7, CREATE) == [201] * 7
    response = await tariff_api.post(URL, json=CREATE, headers=bearer(admin_account))

    assert response.status_code == 409
    assert response.json()["code"] == "connector_limit"


async def test_company_connector_limit_overrides_the_technical_cap(
    tariff_api: httpx.AsyncClient,
    admin_account: User,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    tenant_ctx.tariff = Tariff.EXTENDED.value
    tenant_ctx.connector_limit = 2
    await session.commit()

    assert await _create_many(tariff_api, admin_account, 3, CREATE) == [201, 201, 409]


async def test_non_base_system_only_in_enterprise(
    tariff_api: httpx.AsyncClient,
    admin_account: User,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    await _tariff(session, tenant_ctx, Tariff.EXTENDED)
    refused = await tariff_api.post(
        URL, json=NON_BASE_CREATE, headers=bearer(admin_account)
    )
    assert refused.status_code == 409
    assert refused.json()["code"] == "connector_not_in_tariff"

    await _tariff(session, tenant_ctx, Tariff.ENTERPRISE)
    accepted = await tariff_api.post(
        URL, json=NON_BASE_CREATE, headers=bearer(admin_account)
    )
    assert accepted.status_code == 201


async def test_catalog_marks_what_the_tariff_allows(
    tariff_api: httpx.AsyncClient, admin_account: User
) -> None:
    kinds = (await tariff_api.get(f"{URL}/kinds", headers=bearer(admin_account))).json()

    by_kind = {kind["kind"]: kind for kind in kinds}
    assert by_kind[ORG_SPEC.kind]["base"] is True
    assert by_kind[ORG_SPEC.kind]["available"] is True
    assert by_kind[NON_BASE.kind]["base"] is False
    assert by_kind[NON_BASE.kind]["available"] is False
    assert not any(kind["limit_reached"] for kind in kinds)


async def test_catalog_marks_new_systems_when_five_are_used(
    tariff_api: httpx.AsyncClient, admin_account: User
) -> None:
    for n in range(1, 6):
        await tariff_api.post(URL, json=_system(n), headers=bearer(admin_account))

    kinds = (await tariff_api.get(f"{URL}/kinds", headers=bearer(admin_account))).json()

    by_kind = {kind["kind"]: kind for kind in kinds}
    # К подключённой системе можно добавить ещё подключение, новую — нельзя.
    assert by_kind[ORG_SPEC.kind]["limit_reached"] is False
    assert by_kind["system_5"]["limit_reached"] is False
    assert by_kind["system_6"]["limit_reached"] is True
    assert by_kind["system_6"]["available"] is True


async def test_tariff_allowance_for_admin(
    tariff_api: httpx.AsyncClient, admin_account: User
) -> None:
    await _create_many(tariff_api, admin_account, 2, CREATE)

    await tariff_api.post(URL, json=_system(2), headers=bearer(admin_account))

    body = (await tariff_api.get(f"{URL}/tariff", headers=bearer(admin_account))).json()

    assert body == {
        "tariff": "base",
        "title": "Базовый",
        "connectors": 3,
        "connector_limit": 7,
        "systems": 2,
        "systems_limit": 5,
    }


async def test_tariff_allowance_is_admin_only(
    tariff_api: httpx.AsyncClient, employee: User
) -> None:
    response = await tariff_api.get(f"{URL}/tariff", headers=bearer(employee))
    assert response.status_code == 403


# --- команда: CLI и сервис --------------------------------------------------------


def _service(session: AsyncSession) -> TenantService:
    return TenantService(
        TenantRepository(session),
        UserRepository(session),
        AuditRepository(session),
        session,
    )


async def test_set_tariff_is_audited_and_warns_over_limit(
    tariff_api: httpx.AsyncClient,
    admin_account: User,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    await _tariff(session, tenant_ctx, Tariff.EXTENDED)
    for n in range(1, 7):
        await tariff_api.post(URL, json=_system(n), headers=bearer(admin_account))

    change = await _service(session).set_tariff(tenant_ctx.company_code, Tariff.BASE)

    assert change.tenant.tariff == "base"
    assert change.systems == 6
    assert change.over_tariff
    event = await session.scalar(
        select(AuditEvent).where(AuditEvent.action == "tenant.tariff_changed")
    )
    assert event is not None
    assert event.details == {
        "from": {"tariff": "extended", "connector_limit": None},
        "to": {"tariff": "base", "connector_limit": None},
    }


async def test_set_tariff_counts_systems_not_connections(
    tariff_api: httpx.AsyncClient,
    admin_account: User,
    session: AsyncSession,
    tenant_ctx: Tenant,
) -> None:
    await _tariff(session, tenant_ctx, Tariff.EXTENDED)
    await _create_many(tariff_api, admin_account, 6, CREATE)

    change = await _service(session).set_tariff(tenant_ctx.company_code, Tariff.BASE)

    assert (change.connectors, change.systems) == (6, 1)
    assert not change.over_tariff


async def test_set_tariff_changes_and_resets_the_connector_limit(
    session: AsyncSession, tenant_ctx: Tenant
) -> None:
    service = _service(session)
    code = tenant_ctx.company_code

    raised = await service.set_tariff(code, Tariff.ENTERPRISE, connector_limit=50)
    assert raised.tenant.connector_limit == 50
    kept = await service.set_tariff(code, Tariff.ENTERPRISE)
    assert kept.tenant.connector_limit == 50
    reset = await service.set_tariff(
        code, Tariff.ENTERPRISE, default_connector_limit=True
    )
    assert reset.tenant.connector_limit is None
    assert not reset.over_tariff


def test_cli_tariff_arguments() -> None:
    create = _parser().parse_args(
        ["create-tenant", "--code", "acme", "--name", "A", "--seats", "5"]
        + ["--admin-email", "a@b.ru"]
    )
    assert create.tariff == "base"
    args = _parser().parse_args(
        ["set-tariff", "--code", "acme", "--tariff", "extended"]
        + ["--connector-limit", "40"]
    )
    assert (args.tariff, args.connector_limit) == ("extended", 40)
    with pytest.raises(SystemExit):
        _parser().parse_args(
            ["set-tariff", "--code", "acme", "--tariff", "extended"]
            + ["--connector-limit", "40", "--default-connector-limit"]
        )
    with pytest.raises(SystemExit):
        _parser().parse_args(["set-tariff", "--code", "acme", "--tariff", "gold"])
