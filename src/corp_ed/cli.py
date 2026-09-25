"""Команды для команды Kronto. Запускаются на сервере, не по сети.

    python -m corp_ed.cli create-tenant --code acme --name "ACME" \\
        --admin-email admin@acme.ru [--admin-name "Иван Петров"]
    python -m corp_ed.cli suspend-tenant --code acme
    python -m corp_ed.cli resume-tenant --code acme

Почему CLI, а не HTTP-ручка «суперадмина»: по досье (10.1) компании
подключает команда после созвона. Ручка с правом создавать тенантов
была бы самой ценной целью для атаки на весь сервис, а CLI доступен
только тому, у кого уже есть доступ к серверу и к DATABASE_URL.

Временный пароль администратора печатается один раз в stdout и нигде
не сохраняется. Передавать его клиенту — отдельным каналом от кода
компании; при первом входе система потребует сменить пароль.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence

from corp_ed.core.database import get_session_maker
from corp_ed.core.exceptions import DomainError
from corp_ed.core.logging import configure_logging
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services.tenant_service import TenantService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="corp_ed.cli")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-tenant", help="завести компанию и админа")
    create.add_argument("--code", required=True, help="код компании для входа")
    create.add_argument("--name", required=True, help="название компании")
    create.add_argument("--admin-email", required=True)
    create.add_argument("--admin-name", default=None)

    for name in ("suspend-tenant", "resume-tenant"):
        command = commands.add_parser(name)
        command.add_argument("--code", required=True)

    return parser


async def _run(args: argparse.Namespace) -> int:
    async with get_session_maker()() as session:
        service = TenantService(
            TenantRepository(session), UserRepository(session), session
        )
        if args.command == "create-tenant":
            result = await service.provision(
                company_code=args.code,
                name=args.name,
                admin_email=args.admin_email,
                admin_full_name=args.admin_name,
            )
            print(f"company_code:       {result.tenant.company_code}")
            print(f"tenant_id:          {result.tenant.id}")
            print(f"admin_email:        {result.admin.email}")
            print(f"temporary_password: {result.temporary_password}")
            print("Пароль показан один раз. Сменить при первом входе.")
            return 0

        tenant = await service.set_active(
            args.code, active=args.command == "resume-tenant"
        )
        print(f"{tenant.company_code}: is_active={tenant.is_active}")
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except DomainError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
