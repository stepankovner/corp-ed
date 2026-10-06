"""Отправка писем: Postbox по HTTPS, SMTP, лог или память (ТЗ §3).

Отправляет только воркер (services/mail_worker.py) — из очереди
outbox_emails. Запросы письма не отправляют: медленный или лежащий
почтовый сервер не должен ломать регистрацию.

Postbox (Yandex Cloud) — HTTPS-запрос, совместимый с Amazon SES v2: на
Selectel исходящие порты SMTP закрыты (RISKS №57), порт 443 открыт.
SMTP — стандартный smtplib в отдельном потоке: писем мало (подтверждения,
сбросы, уведомления), асинхронный клиент — лишняя зависимость.
"""

import asyncio
import json
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid
from typing import Protocol

import httpx
import structlog

from corp_ed.core import sigv4
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


POSTBOX_SEND_PATH = "/v2/email/outbound-emails"


class PostboxSender:
    """Yandex Cloud Postbox: POST /v2/email/outbound-emails с подписью
    SigV4 статическим ключом сервисного аккаунта (роль postbox.sender).
    Отправитель — адрес домена, подтверждённого в Postbox (DKIM)."""

    def __init__(self, settings: MailSettings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    async def send(self, email: OutgoingEmail) -> None:
        s = self.settings
        url = s.postbox_url.rstrip("/") + POSTBOX_SEND_PATH
        body = json.dumps(self._payload(email), ensure_ascii=False).encode()
        headers = sigv4.sign(
            method="POST",
            url=url,
            headers={"Content-Type": "application/json"},
            body=body,
            key_id=s.postbox_key_id or "",
            secret=s.postbox_secret_key.get_secret_value()
            if s.postbox_secret_key
            else "",
            region=s.postbox_region,
            service="ses",
            now=datetime.now(UTC),
        )
        try:
            response = await self.client.post(
                url, content=body, headers=headers, timeout=s.smtp_timeout_seconds
            )
        except httpx.HTTPError as exc:
            raise MailDeliveryError("postbox_unavailable", retryable=True) from exc
        status = response.status_code
        if status < 300:
            return
        if status in (401, 403):
            # Ключ, роль postbox.sender или папка не те — повтором не исправить.
            raise MailDeliveryError("postbox_auth", retryable=False)
        # 429 — лимит отправки, 5xx — сбой у Postbox; 4xx — письмо
        # отвергнуто (адрес не подтверждён, неверный получатель).
        raise MailDeliveryError(
            f"postbox_{status}", retryable=status == 429 or status >= 500
        )

    def _payload(self, email: OutgoingEmail) -> dict[str, object]:
        s = self.settings
        return {
            "FromEmailAddress": formataddr((s.from_name, s.from_address)),
            "Destination": {"ToAddresses": [email.to]},
            "Content": {
                "Simple": {
                    "Subject": {"Data": email.subject, "Charset": "UTF-8"},
                    "Body": {
                        "Text": {"Data": email.text, "Charset": "UTF-8"},
                        "Html": {"Data": email.html, "Charset": "UTF-8"},
                    },
                }
            },
        }


def build_sender(
    settings: MailSettings, client: httpx.AsyncClient | None = None
) -> MailSender:
    """client нужен Postbox — HTTP-клиент процесса (воркер, CLI)."""
    if settings.backend == "postbox":
        if client is None:
            raise ValueError("MAIL_BACKEND=postbox needs an HTTP client")
        return PostboxSender(settings, client)
    if settings.backend == "smtp":
        return SmtpSender(settings)
    if settings.backend == "memory":
        return MemorySender()
    return ConsoleSender()
