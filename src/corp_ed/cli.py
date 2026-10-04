"""Команды для команды Kronto. Запускаются на сервере, не по сети.

    python -m corp_ed.cli create-tenant --code acme --name "ACME" --seats 50 \\
        --admin-email admin@acme.ru [--admin-name "Иван Петров"] \\
        [--admin-password-stdin]   # пароль — только если учётки ещё нет
    python -m corp_ed.cli requests list [--status new|all]   # «Подключить компанию»
    python -m corp_ed.cli requests approve --id <uuid> [--seats 30] \\
        [--tariff extended] [--code acme]
    python -m corp_ed.cli requests reject --id <uuid>
    python -m corp_ed.cli reset-password --email admin@acme.ru [--password-stdin]
    python -m corp_ed.cli set-totp --email stand-check@krontoai.ru --secret-stdin
                                       # приложение-аутентификатор служебной учётке
    python -m corp_ed.cli set-seats --code acme --seats 80 [--yes]
    python -m corp_ed.cli set-not-found-mode --code acme --mode general
    python -m corp_ed.cli set-tariff --code acme --tariff extended \
        [--connector-limit 50 | --default-connector-limit]
    python -m corp_ed.cli suspend-tenant --code acme
    python -m corp_ed.cli resume-tenant --code acme
    python -m corp_ed.cli reindex (--code acme | --all) [--dry-run]
    python -m corp_ed.cli purge        # удалить данные старше срока хранения
    python -m corp_ed.cli leads list [--status new]     # заявки на созвон
    python -m corp_ed.cli leads set-status --id <uuid> --status contacted
    python -m corp_ed.cli gaps (--code acme | --all)   # отчёт о пробелах
    python -m corp_ed.cli rotate-connector-secrets     # после смены ключа
    python -m corp_ed.cli connector-check --kind bitrix24 \\
        --config portal=https://b24-xxx.bitrix24.ru/ \\
        --credential webhook="$BITRIX24_TEST_WEBHOOK" \\
        --module disk --module knowledge_base [--fetch 1] [--record DIR]
                                       # адаптер против настоящего источника, без базы

Почему CLI, а не HTTP-ручка «суперадмина»: по досье (10.1) компании
подключает команда после созвона. Ручка с правом создавать тенантов
была бы самой ценной целью для атаки на весь сервис, а CLI доступен
только тому, у кого уже есть доступ к серверу и к DATABASE_URL.

Учётка kronto не зависит от компании (ТЗ §2): если у администратора она
уже есть, create-tenant просто делает её администратором новой компании.
Если нет — временный пароль задаёт оператор (скрытый ввод или
--admin-password-stdin); CLI его не генерирует и не печатает (RISKS
№42). Передавать его клиенту — отдельным каналом от почты; при первом
входе система потребует сменить пароль.
"""

import argparse
import asyncio
import getpass
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import func, select

from corp_ed.connectors.base import AdapterError, AdapterOptions, SourceAdapter
from corp_ed.connectors.registry import UnknownKindError, default_registry
from corp_ed.core import totp
from corp_ed.core.config import (
    ConnectorSettings,
    GapsSettings,
    LLMSettings,
    RagSettings,
    get_billing_settings,
    get_connector_settings,
    get_demo_settings,
    get_lead_settings,
    get_settings,
)
from corp_ed.core.database import get_session_maker
from corp_ed.core.exceptions import DomainError
from corp_ed.core.logging import configure_logging
from corp_ed.core.outbound import (
    OutboundClient,
    OutboundURLError,
    validate_outbound_url,
)
from corp_ed.core.secrets import SecretBox
from corp_ed.domain.leads import CALL_TIMEZONE, LeadStatus
from corp_ed.domain.models import Account, Passkey, StaffMember
from corp_ed.domain.tariffs import DEFAULT_TARIFF, Tariff, plan_for
from corp_ed.domain.types import DEFAULT_NOT_FOUND_MODE, NotFoundMode
from corp_ed.llm.factory import build_llm_gateway
from corp_ed.repositories.account_repository import AccountRepository
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.repositories.lead_repository import LeadRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services.company_request_service import CompanyRequestService
from corp_ed.services.connector_check_service import CheckReport, run_check
from corp_ed.services.connector_secrets_rotation import ConnectorSecretsRotation
from corp_ed.services.demo_service import DemoService
from corp_ed.services.digest_service import DigestService
from corp_ed.services.gap_report_service import GapReportService
from corp_ed.services.lead_service import LeadService
from corp_ed.services.reindex_service import ReindexService
from corp_ed.services.retention_service import RetentionService
from corp_ed.services.seats import seats_check
from corp_ed.services.tenant_service import TenantService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="corp_ed.cli")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-tenant", help="завести компанию и админа")
    create.add_argument(
        "--code", required=True, help="внутренний код компании (для cli и логов)"
    )
    create.add_argument("--name", required=True, help="название компании")
    create.add_argument(
        "--seats", required=True, type=int, help="оплаченные места (пул кредитов)"
    )
    create.add_argument("--admin-email", required=True)
    create.add_argument("--admin-name", default=None)
    create.add_argument(
        "--admin-password-stdin",
        action="store_true",
        help="временный пароль администратора, если учётки ещё нет, — первой "
        "строкой stdin; без флага — скрытый ввод в терминале",
    )
    create.add_argument(
        "--not-found-mode",
        choices=[mode.value for mode in NotFoundMode],
        default=DEFAULT_NOT_FOUND_MODE.value,
        help="нет ответа в документах: общий ответ с пометкой (по умолчанию) или отказ",
    )
    create.add_argument(
        "--tariff",
        choices=[tariff.value for tariff in Tariff],
        default=DEFAULT_TARIFF.value,
        help="тариф: base — «Базовый» (по умолчанию), extended — «Расширенный», "
        "enterprise — «Корпоративный»",
    )

    reset = commands.add_parser(
        "reset-password",
        help="временный пароль учётке, когда письмо восстановления не доходит",
    )
    reset.add_argument("--email", required=True)
    reset.add_argument(
        "--password-stdin",
        action="store_true",
        help="временный пароль — первой строкой stdin; без флага — скрытый "
        "ввод в терминале",
    )

    seats = commands.add_parser("set-seats", help="изменить число оплаченных мест")
    seats.add_argument("--code", required=True)
    seats.add_argument("--seats", required=True, type=int)
    seats.add_argument(
        "--yes",
        action="store_true",
        help="подтвердить, если новый пул меньше уже потраченного за месяц",
    )

    tariff = commands.add_parser(
        "set-tariff", help="тариф компании и потолок подключений"
    )
    tariff.add_argument("--code", required=True)
    tariff.add_argument(
        "--tariff", required=True, choices=[value.value for value in Tariff]
    )
    limit = tariff.add_mutually_exclusive_group()
    limit.add_argument(
        "--connector-limit",
        type=int,
        help="технический потолок подключений этой компании (защита от скрипта)",
    )
    limit.add_argument(
        "--default-connector-limit",
        action="store_true",
        help="вернуть общий потолок CONNECTOR_MAX_PER_TENANT",
    )

    not_found = commands.add_parser(
        "set-not-found-mode", help="ответ, когда в документах ответа нет"
    )
    not_found.add_argument("--code", required=True)
    not_found.add_argument(
        "--mode", required=True, choices=[mode.value for mode in NotFoundMode]
    )

    for name in ("suspend-tenant", "resume-tenant"):
        command = commands.add_parser(name)
        command.add_argument("--code", required=True)

    reindex = commands.add_parser(
        "reindex", help="поставить все материалы в очередь на переиндексацию"
    )
    scope = reindex.add_mutually_exclusive_group(required=True)
    scope.add_argument("--code", help="одна компания")
    scope.add_argument("--all", action="store_true", help="все компании")
    reindex.add_argument(
        "--dry-run", action="store_true", help="только посчитать материалы"
    )

    commands.add_parser("purge", help="удалить данные старше срока хранения")

    set_totp = commands.add_parser(
        "set-totp",
        help="приложение-аутентификатор служебной учётке (проверка стенда): "
        "секрет base32 — первой строкой stdin",
    )
    set_totp.add_argument("--email", required=True)
    set_totp.add_argument("--secret-stdin", action="store_true", required=True)

    digest = commands.add_parser(
        "digest",
        help="недельная сводка администраторам (воркер шлёт её сам по понедельникам)",
    )
    digest.add_argument("--code", help="одна компания")
    digest.add_argument(
        "--force",
        action="store_true",
        help="не ждать понедельника и прислать ещё раз — проверить письмо",
    )

    demo = commands.add_parser(
        "demo", help="песочница на сайте: вымышленная компания и её документы"
    )
    demo_commands = demo.add_subparsers(dest="demo_command", required=True)
    demo_commands.add_parser(
        "setup",
        help="завести компанию песочницы или обновить её документы "
        "(идемпотентно, запускает выкатка)",
    )

    staff = commands.add_parser(
        "staff", help="команда kronto с доступом к нашей панели (/staff)"
    )
    staff_commands = staff.add_subparsers(dest="staff_command", required=True)
    staff_commands.add_parser("list", help="кто в команде")
    for name, text in (("add", "открыть панель"), ("remove", "закрыть панель")):
        staff_command = staff_commands.add_parser(name, help=text)
        staff_command.add_argument("--email", required=True)

    requests = commands.add_parser(
        "requests", help="заявки «Подключить компанию» от учёток без компании"
    )
    requests_commands = requests.add_subparsers(dest="requests_command", required=True)
    requests_list = requests_commands.add_parser("list", help="заявки")
    requests_list.add_argument(
        "--status",
        choices=["all", "new", "approved", "rejected", "cancelled"],
        default="new",
    )
    requests_approve = requests_commands.add_parser(
        "approve", help="создать компанию, заявитель — администратор"
    )
    requests_approve.add_argument("--id", required=True, type=UUID)
    requests_approve.add_argument("--seats", type=int, default=None)
    requests_approve.add_argument(
        "--tariff",
        choices=[tariff.value for tariff in Tariff],
        default=DEFAULT_TARIFF.value,
    )
    requests_approve.add_argument(
        "--code", default=None, help="код компании; без него — из названия"
    )
    requests_reject = requests_commands.add_parser("reject", help="отклонить")
    requests_reject.add_argument("--id", required=True, type=UUID)

    leads = commands.add_parser("leads", help="заявки на созвон со страницы тарифов")
    leads_commands = leads.add_subparsers(dest="leads_command", required=True)
    leads_list = leads_commands.add_parser("list", help="последние заявки")
    leads_list.add_argument(
        "--status",
        choices=["all", *(status.value for status in LeadStatus)],
        default=LeadStatus.NEW.value,
    )
    leads_list.add_argument("--limit", type=int, default=50)
    leads_status = leads_commands.add_parser("set-status", help="отметить заявку")
    leads_status.add_argument("--id", required=True, type=UUID)
    leads_status.add_argument(
        "--status", required=True, choices=[status.value for status in LeadStatus]
    )

    gaps = commands.add_parser(
        "gaps", help="пересобрать отчёт о пробелах (раз в сутки, после purge)"
    )
    gaps_scope = gaps.add_mutually_exclusive_group(required=True)
    gaps_scope.add_argument("--code", help="одна компания")
    gaps_scope.add_argument("--all", action="store_true", help="все активные")

    commands.add_parser(
        "rotate-connector-secrets",
        help="перешифровать учётные данные коннекторов первым ключом "
        "CONNECTOR_SECRETS_KEYS (docs/DEPLOY.md, раздел 9)",
    )

    check = commands.add_parser(
        "connector-check",
        help="проверить адаптер против настоящего источника: check, листинг, "
        "скачивание; база не нужна",
    )
    check.add_argument(
        "--kind", required=True, help="вид из каталога, например bitrix24"
    )
    check.add_argument(
        "--config",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="поле config (адрес портала, client_id); можно повторять",
    )
    check.add_argument(
        "--credential",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="учётные данные (webhook=…, access_token=…); можно повторять",
    )
    check.add_argument(
        "--module", action="append", default=[], help="модуль; можно повторять"
    )
    check.add_argument(
        "--limit", type=int, default=50, help="сколько документов листить"
    )
    check.add_argument(
        "--fetch", type=int, default=1, help="сколько документов каждого модуля скачать"
    )
    check.add_argument(
        "--record",
        metavar="DIR",
        default=None,
        help="записать ответы REST (без токенов) в каталог — фикстуры для тестов",
    )
    check.add_argument(
        "--fast",
        action="store_true",
        help="без паузы между запросами (только для коробки или тестов)",
    )

    return parser


async def _run(args: argparse.Namespace) -> int:
    if args.command == "connector-check":
        return await _connector_check(args)

    if args.command == "purge":
        purged = await RetentionService(
            get_session_maker(),
            get_settings().qa_log_retention_days,
            get_connector_settings().sync_run_retention_days,
            get_lead_settings().retention_days,
        ).purge()
        print(
            f"qa_log: {purged.qa_log}, audit_events: {purged.audit_events}, "
            f"sync_runs: {purged.sync_runs}, leads: {purged.leads}, "
            f"attachments: {purged.attachments}"
        )
        return 0

    if args.command == "leads":
        return await _leads(args)

    if args.command == "requests":
        return await _requests(args)

    if args.command == "staff":
        return await _staff(args)

    if args.command == "demo":
        demo_report = await DemoService(
            get_session_maker(), get_demo_settings()
        ).setup()
        company = "заведена" if demo_report.tenant_created else "есть"
        print(
            f"песочница: компания {company}; документов новых "
            f"{demo_report.created}, обновлено {demo_report.updated}, "
            f"без изменений {demo_report.unchanged}"
        )
        return 0

    if args.command == "digest":
        digest_report = await DigestService(
            get_session_maker(), zone=get_billing_settings().billing_timezone
        ).send_due(force=args.force, company_code=args.code)
        print(
            f"сводок отправлено: {digest_report.sent}, "
            f"пропущено: {digest_report.skipped}"
        )
        return 0

    if args.command == "set-totp":
        return await _set_totp(args.email, sys.stdin.readline().strip())

    if args.command == "gaps":
        return await _gaps(None if args.all else args.code)

    if args.command == "rotate-connector-secrets":
        settings = get_connector_settings()
        rotated = await ConnectorSecretsRotation(
            get_session_maker(), SecretBox(settings.keys)
        ).rotate()
        print(f"connectors: {rotated.connectors}, grants: {rotated.grants}")
        return 0

    if args.command == "reindex":
        reports = await ReindexService(get_session_maker()).reindex(
            company_code=None if args.all else args.code, dry_run=args.dry_run
        )
        for report in reports:
            print(
                f"{report.company_code}: материалов {report.materials}, "
                f"поставлено в очередь {report.queued}"
            )
        return 0

    async with get_session_maker()() as session:
        service = TenantService(
            TenantRepository(session),
            UserRepository(session),
            AuditRepository(session),
            session,
        )
        if args.command == "create-tenant":
            existing = await AccountRepository(session).get_by_email(args.admin_email)
            result = await service.provision(
                company_code=args.code,
                name=args.name,
                admin_email=args.admin_email,
                admin_full_name=args.admin_name,
                admin_password=None
                if existing is not None
                else _read_admin_password(from_stdin=args.admin_password_stdin),
                seats=args.seats,
                not_found_mode=NotFoundMode(args.not_found_mode),
                tariff=Tariff(args.tariff),
            )
            print(f"company_code:       {result.tenant.company_code}")
            print(f"tenant_id:          {result.tenant.id}")
            print(f"seats:              {result.tenant.seats}")
            print(f"not_found_mode:     {result.tenant.not_found_mode}")
            print(f"tariff:             {result.tenant.tariff}")
            print(f"admin_email:        {result.account.email}")
            if result.account_created:
                print(
                    "Учётка заведена, временный пароль задан. Передайте его "
                    "клиенту отдельным каналом; при первом входе система "
                    "потребует сменить его."
                )
            else:
                print("Учётка уже была — она стала администратором компании.")
            return 0

        if args.command == "reset-password":
            account = await service.reset_password(
                args.email,
                _read_admin_password(
                    from_stdin=args.password_stdin,
                    prompt="Временный пароль: ",
                    stdin_flag="--password-stdin",
                ),
            )
            print(f"{account.email}: временный пароль задан, сессии закрыты.")
            print(
                "Передайте пароль отдельным каналом; при входе система "
                "потребует сменить его."
            )
            return 0

        if args.command == "set-seats":
            check = await seats_check(session, args.code, args.seats)
            if check.stops_pool and not args.yes:
                print(
                    f"Внимание: {check.message}\n"
                    "Если так и нужно — повторите команду с --yes.",
                    file=sys.stderr,
                )
                return 1
            tenant = await service.set_seats(args.code, args.seats)
            print(f"{tenant.company_code}: seats={tenant.seats}")
            if check.message:
                print(f"Внимание: {check.message}")
            return 0

        if args.command == "set-tariff":
            change = await service.set_tariff(
                args.code,
                Tariff(args.tariff),
                connector_limit=args.connector_limit,
                default_connector_limit=args.default_connector_limit,
            )
            plan = plan_for(change.tenant.tariff)
            print(
                f"{change.tenant.company_code}: tariff={change.tenant.tariff} "
                f"«{plan.title}», connector_limit="
                f"{change.tenant.connector_limit or 'общий'}"
            )
            if change.over_tariff:
                print(
                    f"Внимание: подключений {change.connectors}, а тариф даёт "
                    f"{plan.max_connectors}. Заведённые продолжат работать, "
                    "новые добавить нельзя."
                )
            return 0

        if args.command == "set-not-found-mode":
            tenant = await service.set_not_found_mode(
                args.code, NotFoundMode(args.mode)
            )
            print(f"{tenant.company_code}: not_found_mode={tenant.not_found_mode}")
            return 0

        tenant = await service.set_active(
            args.code, active=args.command == "resume-tenant"
        )
        print(f"{tenant.company_code}: is_active={tenant.is_active}")
        return 0


async def _leads(args: argparse.Namespace) -> int:
    """Заявки на созвон для команды: кому перезвонить (досье 10.1).

    Вывод — оператору в терминал: имя и телефон нужны, чтобы перезвонить.
    В журналы приложения они не пишутся.
    """
    async with get_session_maker()() as session:
        service = LeadService(LeadRepository(session), session, get_lead_settings())
        if args.leads_command == "set-status":
            lead = await service.set_status(args.id, LeadStatus(args.status))
            print(f"{lead.id}: status={lead.status}")
            return 0
        status = None if args.status == "all" else LeadStatus(args.status)
        found = await service.list_recent(status=status, limit=args.limit)
        for lead in found:
            created = lead.created_at.astimezone(CALL_TIMEZONE)
            print(
                f"{lead.id}  {created:%d.%m %H:%M}  [{lead.status}]  "
                f"{lead.company_name}, {lead.seats} мест, тариф {lead.tariff}"
            )
            contact = ", ".join(
                part for part in (lead.contact_name, lead.phone, lead.email) if part
            )
            print(f"    {contact}")
            print(f"    созвон: {lead.preferred_date:%d.%m} {lead.preferred_slot} МСК")
            if lead.comment:
                print(f"    {lead.comment[:300]}")
        print(f"Заявок: {len(found)}")
        return 0


async def _set_totp(email: str, secret: str) -> int:
    """Секрет TOTP служебной учётке — чтобы автоматическая проверка стенда
    проходила второй фактор (ТЗ §3). Людям — только через настройки."""

    try:
        totp.code_at(secret, 0)
    except ValueError as exc:
        raise DomainError("Секрет — base32 (A–Z, 2–7)") from exc
    box = SecretBox(get_connector_settings().keys)
    async with get_session_maker()() as session:
        account = await AccountRepository(session).get_by_email(email)
        if account is None:
            raise DomainError(f"Учётки {email} нет")
        account.totp_secret = box.encrypt({"secret": secret})
        account.totp_enabled_at = datetime.now(UTC)
        account.totp_last_step = None
        AuditRepository(session).record(
            AuditAction.MFA_ENABLED,
            details={"account_id": str(account.id), "method": "totp", "source": "cli"},
        )
        await session.commit()
    print(f"{email}: приложение-аутентификатор включено.")
    return 0


async def _staff(args: argparse.Namespace) -> int:
    """Наша панель (ТЗ §9): кто в команде. Через API в команду не попасть —
    только отсюда, с сервера. Вход в панель — с приложением или ключом."""
    async with get_session_maker()() as session:
        if args.staff_command == "list":
            rows = await session.execute(
                select(Account.email, StaffMember.added_at)
                .join(StaffMember, StaffMember.account_id == Account.id)
                .order_by(Account.email)
            )
            for email, added in rows:
                print(f"{email}\t{added:%Y-%m-%d}")
            return 0
        account = await AccountRepository(session).get_by_email(args.email)
        if account is None:
            raise DomainError(f"Учётки {args.email} нет: сначала регистрация")
        member = await session.get(StaffMember, account.id)
        if args.staff_command == "add":
            if member is None:
                session.add(StaffMember(account_id=account.id))
                AuditRepository(session).record(
                    AuditAction.STAFF_ADDED,
                    details={"account_id": str(account.id), "source": "cli"},
                )
                await session.commit()
            strong = account.totp_enabled_at is not None or bool(
                await session.scalar(
                    select(func.count()).where(Passkey.account_id == account.id)
                )
            )
            print(f"{account.email}: в команде.")
            if not strong:
                print(
                    "Панель откроется после того, как он включит приложение-"
                    "аутентификатор или ключ доступа (Настройки → Безопасность)."
                )
            return 0
        if member is not None:
            await session.delete(member)
            AuditRepository(session).record(
                AuditAction.STAFF_REMOVED,
                details={"account_id": str(account.id), "source": "cli"},
            )
            await session.commit()
        print(f"{account.email}: не в команде.")
        return 0


async def _requests(args: argparse.Namespace) -> int:
    async with get_session_maker()() as session:
        service = CompanyRequestService(session, AuditRepository(session))
        if args.requests_command == "approve":
            tenant = await service.approve(
                args.id,
                seats=args.seats,
                tariff=Tariff(args.tariff),
                company_code=args.code,
            )
            print(
                f"Компания создана: {tenant.name} (код {tenant.company_code}, "
                f"мест {tenant.seats}, тариф {tenant.tariff}). Заявителю ушло письмо."
            )
            return 0
        if args.requests_command == "reject":
            await service.reject(args.id)
            print("Заявка отклонена, заявителю ушло письмо.")
            return 0
        found = await service.list(None if args.status == "all" else args.status)
        accounts = AccountRepository(session)
        for request in found:
            account = await accounts.get(request.account_id)
            who = account.email if account else "учётка удалена"
            print(
                f"{request.id}  {request.created_at:%d.%m.%Y %H:%M}  "
                f"[{request.status}]  {request.company_name}  "
                f"мест: {request.seats or '—'}  {who}"
            )
            if request.comment:
                print(f"    {request.comment[:300]}")
        print(f"Заявок: {len(found)}")
        return 0


def _read_admin_password(
    *,
    from_stdin: bool,
    prompt: str = "Временный пароль администратора: ",
    stdin_flag: str = "--admin-password-stdin",
) -> str:
    """Временный пароль — от оператора, не от программы.

    CLI не генерирует, не печатает и не хранит пароль (RISKS №42): его
    нельзя увидеть в выводе, истории терминала или журнале CI. Скрытый
    ввод дважды в терминале или первая строка stdin (как `docker login
    --password-stdin`). Политику проверяет TenantService.
    """
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    if not sys.stdin.isatty():
        raise DomainError(
            f"Нет терминала для ввода пароля: передайте его через {stdin_flag}"
        )
    password = getpass.getpass(prompt)
    if getpass.getpass("Повторите пароль: ") != password:
        raise DomainError("Пароли не совпадают")
    if not password.isascii():
        # Скрытый ввод не показывает раскладку: русская превращает пароль
        # в кириллицу, а клиент наберёт его латиницей (стенд 02.10).
        raise DomainError(
            "В пароле есть не латинские символы — проверьте раскладку "
            "клавиатуры и введите снова"
        )
    return password


async def _connector_check(
    args: argparse.Namespace, http: OutboundClient | None = None
) -> int:
    settings = ConnectorSettings()
    registry = default_registry(settings)
    try:
        spec = registry.spec(args.kind)
    except UnknownKindError:
        print(f"Ошибка: неизвестный вид {args.kind}", file=sys.stderr)
        return 2
    config = _pairs(args.config)
    credentials = _pairs(args.credential)
    modules = args.module or [module.name for module in spec.modules]
    unknown = sorted(set(modules) - spec.module_names)
    if unknown:
        print(f"Ошибка: неизвестные модули {', '.join(unknown)}", file=sys.stderr)
        return 2
    if spec.url_field and spec.url_field in config:
        try:
            target = await validate_outbound_url(config[spec.url_field])
        except OutboundURLError as exc:
            print(f"Ошибка: адрес системы не принят ({exc.code})", file=sys.stderr)
            return 2
        config[spec.url_field] = target.url

    recorder = _FixtureRecorder(Path(args.record)) if args.record else None
    async with httpx.AsyncClient() as client:
        outbound = http or OutboundClient(client, via_proxy=settings.outbound_via_proxy)
        adapter: SourceAdapter
        try:
            adapter = registry.build(
                spec.kind,
                config,
                credentials,
                outbound,
                AdapterOptions(recorder=recorder, fast=args.fast),
            )
        except AdapterError as exc:
            print(f"Ошибка сборки адаптера: {exc.code}", file=sys.stderr)
            return 1
        report = await run_check(
            adapter,
            modules=modules,
            limit=args.limit,
            fetch=args.fetch,
            max_bytes=settings.max_document_bytes,
        )
    _print_report(report, modules)
    if recorder is not None:
        print(f"записано ответов: {recorder.count} → {recorder.directory}")
    return 0 if report.ok else 1


def _pairs(values: Sequence[str]) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for item in values:
        name, sep, value = item.partition("=")
        if not sep or not name.strip():
            raise DomainError(f"Ожидается NAME=VALUE, получено: {item}")
        pairs[name.strip()] = value
    return pairs


def _print_report(report: CheckReport, modules: Sequence[str]) -> None:
    print(f"check: {'ok' if report.check_ok else 'ОШИБКА ' + str(report.check_code)}")
    if not report.check_ok:
        return
    by_module = {m: 0 for m in modules}
    for document in report.documents:
        by_module[document.module] = by_module.get(document.module, 0) + 1
    suffix = " (обрезано по --limit)" if report.truncated else ""
    print(f"документов: {len(report.documents)}{suffix}; по модулям: {by_module}")
    if report.list_error_code:
        print(f"листинг прерван: {report.list_error_code}")
    for document in report.documents:
        size = f"{document.size} Б" if document.size is not None else "-"
        print(
            f"  [{document.module}] {document.external_id} «{document.title}» "
            f"{size} v={document.version} {document.path} → {document.url}"
        )
    for fetched in report.fetched:
        if fetched.ok:
            print(
                f"скачано {fetched.external_id}: {fetched.format}, {fetched.size} Б, "
                f"текста {fetched.chars} симв., sha256 {fetched.sha256}"
            )
        else:
            print(f"скачать {fetched.external_id} не удалось: {fetched.error_code}")


class _FixtureRecorder:
    """Ответы REST по одному файлу на вызов: NNN-method.json."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.count = 0
        directory.mkdir(parents=True, exist_ok=True)

    def __call__(self, method: str, params: Mapping[str, Any], response: Any) -> None:
        self.count += 1
        # Имя метода Битрикс24 или путь REST Confluence/Яндекса → имя файла.
        safe = "".join(ch if ch.isalnum() or ch in ".-" else "_" for ch in method)
        path = self.directory / f"{self.count:03d}-{safe}.json"
        path.write_text(
            json.dumps(
                {"method": method, "params": params, "response": response},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


async def _gaps(company_code: str | None) -> int:
    llm_settings = LLMSettings()
    rag = RagSettings()  # type: ignore[call-arg]
    async with httpx.AsyncClient() as client:
        service = GapReportService(
            get_session_maker(),
            build_llm_gateway(client, llm_settings),
            GapsSettings(),  # type: ignore[call-arg]
            max_distance=rag.faq_max_distance,
        )
        reports = await service.run(company_code)
    for report in reports:
        status = "ОШИБКА" if report.failed else "ok"
        print(
            f"{report.company_code}: {status}, вопросов {report.window}, "
            f"кандидатов {report.candidates}, пробелов {report.clusters}, "
            f"подписано {report.labeled}"
        )
    return 1 if any(report.failed for report in reports) else 0


def main(argv: Sequence[str] | None = None) -> int:
    # Сначала аргументы: `--help` и ошибка в них не должны требовать
    # SECRET_KEY и DATABASE_URL, которые читает настройка логирования.
    args = _parser().parse_args(argv)
    configure_logging()
    try:
        return asyncio.run(_run(args))
    except DomainError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
