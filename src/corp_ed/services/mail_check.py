"""Проверка почты стенда (cli mail-check, deploy/stage/check.sh).

Письмо уходит не в запросе, а из очереди воркером (mail_worker.py),
поэтому «не пришло» снаружи не видно: ни регистрация, ни сквозная
проверка стенда (вход с приложением) об этом не узнают. Здесь — то, что
видно с сервера: включена ли отправка, пускает ли ящик, что с очередью.

В выводе нет адресов получателей: он попадает в публичный лог выкатки.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.config import MailSettings
from corp_ed.core.mail import MailDeliveryError, SmtpSender
from corp_ed.repositories.outbox_repository import OutboxGroup, OutboxRepository

STUCK_AFTER = timedelta(minutes=10)
"""Письмо в очереди дольше — воркер не отправляет или почта отвергает."""
HARMLESS_ERRORS = frozenset({"recipient_refused"})
"""Опечатка в адресе у человека — не поломка почты стенда."""


@dataclass
class MailCheckReport:
    backend: str
    server: str | None = None
    login: str | None = None
    problems: list[str] = field(default_factory=list)
    sent: int = 0
    pending: int = 0
    failed: int = 0
    errors: Counter[str] = field(default_factory=Counter)
    days: int = 7

    @property
    def ok(self) -> bool:
        return not self.problems

    def lines(self) -> list[str]:
        mark = "OK  " if self.ok else "FAIL"
        head = f"{mark} почта: {self.backend}"
        if self.server:
            head += f" {self.server}"
        if self.login:
            head += f", вход в ящик — {self.login}"
        head += (
            f"; за {self.days} дн.: отправлено {self.sent}, ждут {self.pending}, "
            f"не отправлено {self.failed}"
        )
        if self.errors:
            counts = sorted(self.errors.items())
            head += f" ({', '.join(f'{error} ×{count}' for error, count in counts)})"
        return [head, *(f"     {problem}" for problem in self.problems)]


async def check_mail(
    settings: MailSettings,
    session_maker: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    days: int = 7,
    sender: SmtpSender | None = None,
) -> MailCheckReport:
    report = MailCheckReport(backend=settings.backend, days=days)
    if settings.backend in ("console", "memory"):
        report.problems.append(
            "отправка выключена: письма только в журнал воркера — "
            "MAIL_* в .env сервера (STAGE.md §4.5а)"
        )
    elif settings.backend == "postbox":
        # Проверить ключ без письма Postbox не даёт (роли postbox.sender
        # доступна только отправка): ошибки видны по очереди ниже, живая
        # проверка — cli mail-check --send-to.
        report.server = f"{settings.postbox_url} {settings.postbox_region}"
    else:
        report.server = (
            f"{settings.smtp_host}:{settings.smtp_port} {settings.smtp_security}"
        )
        report.login = await _probe(sender or SmtpSender(settings))
        if report.login != "ok":
            report.problems.append(_LOGIN_HINTS.get(report.login, report.login))
        if not (settings.smtp_username and settings.smtp_password):
            report.problems.append(
                "нет MAIL_SMTP_USERNAME или MAIL_SMTP_PASSWORD: "
                "Яндекс не отправляет без входа"
            )
        elif settings.smtp_username.lower() != settings.from_address.lower():
            report.problems.append(
                "MAIL_FROM_ADDRESS не совпадает с MAIL_SMTP_USERNAME: Яндекс "
                "отвергает письма от чужого адреса (553)"
            )

    async with session_maker() as session:
        groups = await OutboxRepository(session).summary(now - timedelta(days=days))
    _count(report, groups, now)
    return report


async def _probe(sender: SmtpSender) -> str:
    try:
        await sender.probe()
    except MailDeliveryError as exc:
        return exc.code
    return "ok"


_LOGIN_HINTS = {
    "smtp_auth": "ящик не пускает: неверный пароль приложения или в ящике "
    "не включён доступ почтовых программ (STAGE.md §4.5а, шаги 2–3)",
    "smtp_unavailable": "до почтового сервера нет связи с этого сервера: на "
    "Selectel исходящие 25, 465 и 587 закрыты — MAIL_BACKEND=postbox "
    "(STAGE.md §4.5а); иначе сеть или имя MAIL_SMTP_HOST",
}


def _count(report: MailCheckReport, groups: list[OutboxGroup], now: datetime) -> None:
    for group in groups:
        if group.state == "sent":
            report.sent += group.count
            continue
        if group.state == "failed":
            report.failed += group.count
        else:
            report.pending += group.count
        if group.last_error:
            report.errors[group.last_error] += group.count
        if group.state == "failed" and group.last_error not in HARMLESS_ERRORS:
            report.problems.append(
                f"не отправлено {group.count} ({group.kind}): {group.last_error}"
            )
        if group.state == "pending" and now - group.oldest > STUCK_AFTER:
            minutes = int((now - group.oldest).total_seconds() // 60)
            report.problems.append(
                f"{group.count} ({group.kind}) в очереди дольше "
                f"{int(STUCK_AFTER.total_seconds() // 60)} мин: старейшее — "
                f"{minutes} мин, попыток до {group.max_attempts}"
                + (f", ошибка {group.last_error}" if group.last_error else "")
            )
