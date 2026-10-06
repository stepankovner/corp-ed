"""Отправка писем: SMTP, лог или память (ТЗ §3).

Отправляет только воркер (services/mail_worker.py) — из очереди
outbox_emails. Запросы письма не отправляют: медленный или лежащий
почтовый сервер не должен ломать регистрацию.

SMTP — стандартный smtplib в отдельном потоке: писем мало (подтверждения,
сбросы, уведомления), асинхронный клиент — лишняя зависимость.
"""

import asyncio
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from typing import Protocol

import structlog

from corp_ed.core.config import MailSettings

logger = structlog.get_logger()


@dataclass(frozen=True)
class OutgoingEmail:
    to: str
    subject: str
    text: str
    html: str


class MailDeliveryError(Exception):
    """Письмо не ушло. retryable — стоит ли повторять (сеть, 4xx SMTP)."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class MailSender(Protocol):
    async def send(self, email: OutgoingEmail) -> None: ...


class ConsoleSender:
    """Разработка: письмо — в лог. В production не используется."""

    async def send(self, email: OutgoingEmail) -> None:
        logger.info("mail_console", to=email.to, subject=email.subject, text=email.text)


class MemorySender:
    """Тесты: письма копятся в списке."""

    def __init__(self) -> None:
        self.sent: list[OutgoingEmail] = []

    async def send(self, email: OutgoingEmail) -> None:
        self.sent.append(email)


class SmtpSender:
    def __init__(self, settings: MailSettings) -> None:
        self.settings = settings

    async def send(self, email: OutgoingEmail) -> None:
        message = self._message(email)
        await asyncio.to_thread(self._deliver, message)

    def _message(self, email: OutgoingEmail) -> EmailMessage:
        s = self.settings
        message = EmailMessage()
        username, _, domain = s.from_address.partition("@")
        message["From"] = Address(s.from_name, username, domain)
        message["To"] = email.to
        message["Subject"] = email.subject
        message["Date"] = format_datetime(datetime.now(UTC))
        message["Message-ID"] = make_msgid(domain=domain or None)
        # Служебные письма: автоответчики не должны отвечать на них.
        message["Auto-Submitted"] = "auto-generated"
        message.set_content(email.text)
        message.add_alternative(email.html, subtype="html")
        return message

    async def probe(self) -> None:
        """Войти в ящик и выйти, ничего не отправляя (cli mail-check):
        сеть до сервера, TLS и пароль приложения — без письма."""
        await asyncio.to_thread(self._deliver, None)

    def _deliver(self, message: EmailMessage | None) -> None:
        s = self.settings
        host = s.smtp_host or ""
        try:
            if s.smtp_security == "ssl":
                client: smtplib.SMTP = smtplib.SMTP_SSL(
                    host,
                    s.smtp_port,
                    timeout=s.smtp_timeout_seconds,
                    context=ssl.create_default_context(),
                )
            else:
                client = smtplib.SMTP(host, s.smtp_port, timeout=s.smtp_timeout_seconds)
            with client:
                if s.smtp_security == "starttls":
                    client.starttls(context=ssl.create_default_context())
                if s.smtp_username and s.smtp_password:
                    client.login(s.smtp_username, s.smtp_password.get_secret_value())
                if message is not None:
                    client.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            # Неверный пароль приложения повтором не исправить.
            raise MailDeliveryError("smtp_auth", retryable=False) from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise MailDeliveryError("recipient_refused", retryable=False) from exc
        except smtplib.SMTPResponseException as exc:
            # 4xx — временно (лимит, серый список), 5xx — окончательно.
            code, retryable = f"smtp_{exc.smtp_code}", 400 <= exc.smtp_code < 500
            raise MailDeliveryError(code, retryable=retryable) from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise MailDeliveryError("smtp_unavailable", retryable=True) from exc


def build_sender(settings: MailSettings) -> MailSender:
    if settings.backend == "smtp":
        return SmtpSender(settings)
    if settings.backend == "memory":
        return MemorySender()
    return ConsoleSender()
