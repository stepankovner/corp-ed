"""Постановка писем в очередь (ТЗ §3).

Письмо кладётся в outbox_emails в транзакции действия: регистрация и
письмо с кодом фиксируются вместе, откат действия убирает и письмо.
Отправляет воркер (services/mail_worker.py).
"""

from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.config import MailSettings, get_mail_settings
from corp_ed.domain.models import OutboxEmail
from corp_ed.repositories.outbox_repository import OutboxRepository
from corp_ed.services.email_templates import RenderedEmail


class EmailService:
    def __init__(self, session: AsyncSession, settings: MailSettings | None = None):
        self.outbox = OutboxRepository(session)
        self.settings = settings or get_mail_settings()

    def url(self, path: str) -> str:
        """Абсолютная ссылка на страницу сайта для письма."""
        return f"{self.settings.site_url}{path}"

    def enqueue(self, to: str, email: RenderedEmail) -> None:
        self.outbox.add(
            OutboxEmail(
                to_email=to,
                kind=email.kind,
                subject=email.subject,
                text_body=email.text,
                html_body=email.html,
            )
        )
