"""Компания для нагрузочной проверки (docs/LOAD-TEST.md) — ТОЛЬКО локально.

Заводит компанию с администратором и пулом сотрудников прямо в базе и
пишет их идентификаторы в JSON: токены доступа выпускает генератор
нагрузки сам (run.py) ключом SECRET_KEY — второй фактор и вход в нагрузке
не участвуют. Администратору включается приложение-аутентификатор со
случайным секретом: без него ручки компании отвечают 403.

    DATABASE_URL=<владелец схемы> SECRET_KEY=… CONNECTOR_SECRETS_KEYS=… \\
        uv run python tools/loadtest/seed.py --users 1000 --out users.json

На стенде и на бою не запускать: пароль у всех учёток одинаковый, а
токены выпускаются без входа.
"""

import argparse
import asyncio
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from corp_ed.core.config import get_connector_settings, get_settings
from corp_ed.core.secrets import SecretBox
from corp_ed.core.security import hash_password
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import Account, MemberStatus, Tenant, User, UserRole
from corp_ed.domain.tariffs import Tariff
from corp_ed.repositories.audit_repository import AuditRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services.tenant_service import TenantService

COMPANY = "loadtest"
DOMAIN = "loadtest.krontoai.ru"  # .local и .test вход не принимает


async def seed(session: AsyncSession, users: int) -> dict[str, object]:
    if await session.scalar(select(Tenant).where(Tenant.company_code == COMPANY)):
        raise SystemExit(f"компания {COMPANY} уже есть — новая база или DROP")
    password = secrets.token_urlsafe(18)
    service = TenantService(
        TenantRepository(session),
        UserRepository(session),
        AuditRepository(session),
        session,
    )
    provisioned = await service.provision(
        company_code=COMPANY,
        name="Нагрузочная проверка",
        admin_email=f"admin@{DOMAIN}",
        admin_full_name="Админ Нагрузки",
        admin_password=password,
        seats=users + 10,
        tariff=Tariff.ENTERPRISE,
        commit=False,
    )
    tenant = provisioned.tenant
    admin_account = provisioned.account
    admin_account.must_change_password = False
    admin_account.totp_secret = SecretBox(get_connector_settings().keys).encrypt(
        {"secret": secrets.token_hex(10)}
    )
    admin_account.totp_enabled_at = datetime.now(UTC)

    hashed = hash_password(password)  # один хеш на всех: argon2 — секунды на тысячу
    accounts = [
        Account(
            email=f"user{i:05d}@{DOMAIN}",
            hashed_password=hashed,
            first_name="Сотрудник",
            last_name=f"{i:05d}",
            email_verified_at=datetime.now(UTC),
        )
        for i in range(users)
    ]
    members = [
        User(
            tenant_id=tenant.id,
            role=UserRole.EMPLOYEE,
            status=MemberStatus.ACTIVE,
            account=account,
        )
        for account in accounts
    ]
    with tenant_scope(tenant.id):
        session.add_all(members)
        await session.commit()

    def entry(account: Account, member: User) -> dict[str, object]:
        return {
            "account_id": str(account.id),
            "account_version": account.token_version,
            "member_id": str(member.id),
            "member_version": member.token_version,
            "role": member.role.value,
        }

    return {
        "tenant_id": str(tenant.id),
        "admin": entry(admin_account, provisioned.admin),
        "employees": [entry(a, m) for a, m in zip(accounts, members, strict=True)],
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--users", type=int, default=1000)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if get_settings().environment == "production":
        raise SystemExit("только для локальной проверки")
    engine = create_async_engine(get_settings().database_url)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        data = await seed(session, args.users)
    await engine.dispose()
    args.out.write_text(json.dumps(data, ensure_ascii=False))
    print(f"компания {COMPANY}: администратор и {args.users} сотрудников → {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
