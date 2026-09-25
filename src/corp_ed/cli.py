"""Команды для команды Kronto. Запускаются на сервере, не по сети.

    python -m corp_ed.cli create-tenant --code acme --name "ACME" --seats 50 \\
        --admin-email admin@acme.ru [--admin-name "Иван Петров"]
    python -m corp_ed.cli set-seats --code acme --seats 80
    python -m corp_ed.cli set-not-found-mode --code acme --mode strict
    python -m corp_ed.cli suspend-tenant --code acme
    python -m corp_ed.cli resume-tenant --code acme
    python -m corp_ed.cli reindex (--code acme | --all) [--dry-run]
    python -m corp_ed.cli purge        # удалить данные старше срока хранения
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

Временный пароль администратора печатается один раз в stdout и нигде
не сохраняется. Передавать его клиенту — отдельным каналом от кода
компании; при первом входе система потребует сменить пароль.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

from corp_ed.connectors.base import AdapterError, AdapterOptions, SourceAdapter
from corp_ed.connectors.registry import UnknownKindError, default_registry
from corp_ed.core.config import (
    ConnectorSettings,
    GapsSettings,
    LLMSettings,
    RagSettings,
    get_connector_settings,
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
from corp_ed.domain.types import NotFoundMode
from corp_ed.llm.factory import build_llm_gateway
from corp_ed.repositories.audit_repository import AuditRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services.connector_check_service import CheckReport, run_check
from corp_ed.services.connector_secrets_rotation import ConnectorSecretsRotation
from corp_ed.services.gap_report_service import GapReportService
from corp_ed.services.reindex_service import ReindexService
from corp_ed.services.retention_service import RetentionService
from corp_ed.services.tenant_service import TenantService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="corp_ed.cli")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-tenant", help="завести компанию и админа")
    create.add_argument("--code", required=True, help="код компании для входа")
    create.add_argument("--name", required=True, help="название компании")
    create.add_argument(
        "--seats", required=True, type=int, help="оплаченные места (пул кредитов)"
    )
    create.add_argument("--admin-email", required=True)
    create.add_argument("--admin-name", default=None)
    create.add_argument(
        "--not-found-mode",
        choices=[mode.value for mode in NotFoundMode],
        default=NotFoundMode.GENERAL.value,
        help="нет ответа в документах: общий ответ с пометкой или отказ",
    )

    seats = commands.add_parser("set-seats", help="изменить число оплаченных мест")
    seats.add_argument("--code", required=True)
    seats.add_argument("--seats", required=True, type=int)

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
        ).purge()
        print(
            f"qa_log: {purged.qa_log}, audit_events: {purged.audit_events}, "
            f"sync_runs: {purged.sync_runs}"
        )
        return 0

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
            result = await service.provision(
                company_code=args.code,
                name=args.name,
                admin_email=args.admin_email,
                admin_full_name=args.admin_name,
                seats=args.seats,
                not_found_mode=NotFoundMode(args.not_found_mode),
            )
            print(f"company_code:       {result.tenant.company_code}")
            print(f"tenant_id:          {result.tenant.id}")
            print(f"seats:              {result.tenant.seats}")
            print(f"not_found_mode:     {result.tenant.not_found_mode}")
            print(f"admin_email:        {result.admin.email}")
            print(f"temporary_password: {result.temporary_password}")
            print("Пароль показан один раз. Сменить при первом входе.")
            return 0

        if args.command == "set-seats":
            tenant = await service.set_seats(args.code, args.seats)
            print(f"{tenant.company_code}: seats={tenant.seats}")
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
    llm_settings = LLMSettings()  # type: ignore[call-arg]
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
