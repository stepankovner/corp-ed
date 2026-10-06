"""Письма: очередь, отправка воркером, шаблоны, SMTP (ТЗ §3)."""

import smtplib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.config import MailSettings
from corp_ed.core.mail import (
    MailDeliveryError,
    MemorySender,
    OutgoingEmail,
    SmtpSender,
    build_sender,
)
from corp_ed.domain.models import OutboxEmail
from corp_ed.services import email_templates
from corp_ed.services.email_service import EmailService
from corp_ed.services.mail_check import check_mail
from corp_ed.services.mail_worker import MAX_ATTEMPTS, MailWorker, retry_delay


class FailingSender:
    def __init__(self, error: MailDeliveryError) -> None:
        self.error = error

    async def send(self, email: OutgoingEmail) -> None:
        raise self.error


async def _enqueue(session: AsyncSession, to: str = "anna@acme.ru") -> None:
    EmailService(session, MailSettings(site_url="https://krontoai.ru/")).enqueue(
        to,
        email_templates.verify_email(
            name="Анна", code="123456", url="https://krontoai.ru/verify", minutes=30
        ),
    )
    await session.commit()


async def _outbox(session: AsyncSession) -> OutboxEmail:
    email = (await session.scalars(select(OutboxEmail))).one()
    await session.refresh(email)
    return email


async def test_worker_sends_and_erases_the_body(
    session: AsyncSession, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    await _enqueue(session)
    sender = MemorySender()
    worker = MailWorker(session_maker, sender)

    assert await worker.run_once() is True
    assert await worker.run_once() is False

    [sent] = sender.sent
    assert sent.to == "anna@acme.ru"
    assert "123456" in sent.text
    stored = await _outbox(session)
    assert stored.sent_at is not None
    # Коды и ссылки после отправки в базе не хранятся.
    assert stored.text_body == stored.html_body == ""


async def test_temporary_failure_is_retried_later(
    session: AsyncSession, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    await _enqueue(session)
    worker = MailWorker(
        session_maker, FailingSender(MailDeliveryError("smtp_451", retryable=True))
    )

    await worker.run_once()

    stored = await _outbox(session)
    assert stored.sent_at is None and stored.failed_at is None
    assert stored.attempts == 1
    assert stored.last_error == "smtp_451"
    assert stored.next_attempt_at > datetime.now(UTC) + timedelta(seconds=20)
    # До следующей попытки письмо не берётся.
    assert await worker.run_once() is False


async def test_permanent_failure_stops_retries(
    session: AsyncSession, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    await _enqueue(session)
    worker = MailWorker(
        session_maker, FailingSender(MailDeliveryError("smtp_auth", retryable=False))
    )

    await worker.run_once()

    stored = await _outbox(session)
    assert stored.failed_at is not None
    assert stored.text_body == ""


async def test_retries_end_after_max_attempts(
    session: AsyncSession, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    await _enqueue(session)
    stored = await _outbox(session)
    stored.attempts = MAX_ATTEMPTS - 1
    await session.commit()
    worker = MailWorker(
        session_maker,
        FailingSender(MailDeliveryError("smtp_unavailable", retryable=True)),
    )

    await worker.run_once()

    assert (await _outbox(session)).failed_at is not None


def test_retry_delay_grows_and_is_capped() -> None:
    assert retry_delay(1) == timedelta(seconds=30)
    assert retry_delay(2) == timedelta(minutes=1)
    assert retry_delay(20) == timedelta(hours=1)


def test_templates_escape_what_people_type() -> None:
    email = email_templates.company_approved(
        name='<img src=x onerror="alert(1)">',
        company="<b>Ромашка</b>",
        url="https://krontoai.ru/",
    )
    assert "<img" not in email.html
    assert "<b>Ромашка</b>" not in email.html
    assert "&lt;b&gt;Ромашка&lt;/b&gt;" in email.html
    # Текстовая версия — как есть: её никто не исполняет.
    assert "<b>Ромашка</b>" in email.text


def test_every_template_has_text_and_html() -> None:
    for email in (
        email_templates.verify_email(name=None, code="000001", url="u", minutes=30),
        email_templates.account_exists(name="А", login_url="l", reset_url="r"),
        email_templates.reset_password(name="А", url="u", minutes=60),
        email_templates.confirm_new_email(
            name="А", new_email="a@b.ru", url="u", hours=24
        ),
        email_templates.email_changed(
            name="А", new_email="a@b.ru", revert_url="u", days=7
        ),
        email_templates.password_changed(name="А", reset_url="u"),
        email_templates.company_approved(name="А", company="К", url="u"),
        email_templates.company_rejected(name="А", company="К"),
    ):
        assert email.subject and email.text and email.html.startswith("<!doctype html>")


def test_smtp_backend_needs_a_host() -> None:
    with pytest.raises(ValidationError):
        MailSettings(backend="smtp")
    assert isinstance(build_sender(MailSettings(backend="memory")), MemorySender)


def test_smtp_message_headers() -> None:
    sender = SmtpSender(
        MailSettings(
            backend="smtp",
            smtp_host="smtp.example.ru",
            from_address="noreply@krontoai.ru",
            from_name="kronto",
        )
    )
    message = sender._message(
        OutgoingEmail(to="anna@acme.ru", subject="Тема", text="Текст", html="<p>Т</p>")
    )
    assert message["From"] == "kronto <noreply@krontoai.ru>"
    assert message["Auto-Submitted"] == "auto-generated"
    assert message.is_multipart()


@pytest.mark.parametrize(
    ("error", "code", "retryable"),
    [
        (smtplib.SMTPAuthenticationError(535, b"bad"), "smtp_auth", False),
        (smtplib.SMTPResponseException(451, b"later"), "smtp_451", True),
        (smtplib.SMTPResponseException(550, b"no"), "smtp_550", False),
        (OSError("network"), "smtp_unavailable", True),
    ],
)
def test_smtp_errors_are_classified(
    monkeypatch: pytest.MonkeyPatch, error: Exception, code: str, retryable: bool
) -> None:
    def broken(*args: object, **kwargs: object) -> smtplib.SMTP:
        raise error

    monkeypatch.setattr(smtplib, "SMTP_SSL", broken)
    sender = SmtpSender(MailSettings(backend="smtp", smtp_host="smtp.example.ru"))
    message = sender._message(
        OutgoingEmail(to="a@b.ru", subject="s", text="t", html="h")
    )

    with pytest.raises(MailDeliveryError) as caught:
        sender._deliver(message)
    assert (caught.value.code, caught.value.retryable) == (code, retryable)


def test_probe_logs_in_without_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class FakeSmtp:
        def __init__(self, *args: object, **kwargs: object) -> None:
            calls.append("connect")

        def __enter__(self) -> "FakeSmtp":
            return self

        def __exit__(self, *args: object) -> None:
            calls.append("quit")

        def login(self, user: str, password: str) -> None:
            calls.append(f"login {user}")

        def send_message(self, message: object) -> None:
            calls.append("send")

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSmtp)
    sender = SmtpSender(
        MailSettings(
            backend="smtp",
            smtp_host="smtp.example.ru",
            smtp_username="noreply@krontoai.ru",
            smtp_password="app-password",  # type: ignore[arg-type]
        )
    )

    sender._deliver(None)

    assert calls == ["connect", "login noreply@krontoai.ru", "quit"]


def _smtp_settings(**overrides: object) -> MailSettings:
    values: dict[str, object] = {
        "backend": "smtp",
        "smtp_host": "smtp.example.ru",
        "smtp_username": "noreply@krontoai.ru",
        "smtp_password": "app-password",
        "from_address": "noreply@krontoai.ru",
    }
    values.update(overrides)
    return MailSettings(**values)  # type: ignore[arg-type]


class ProbeSender(SmtpSender):
    def __init__(self, error: MailDeliveryError | None = None) -> None:
        super().__init__(_smtp_settings())
        self.error = error

    async def probe(self) -> None:
        if self.error is not None:
            raise self.error


async def test_mail_check_reports_queue_without_addresses(
    session: AsyncSession, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    now = datetime.now(UTC)
    session.add_all(
        [
            OutboxEmail(
                to_email="anna@acme.ru",
                kind="verify_email",
                subject="s",
                text_body="",
                html_body="",
                sent_at=now,
            ),
            OutboxEmail(
                to_email="boris@acme.ru",
                kind="invite",
                subject="s",
                text_body="",
                html_body="",
                failed_at=now,
                last_error="smtp_553",
            ),
            OutboxEmail(
                to_email="typo@acme",
                kind="invite",
                subject="s",
                text_body="",
                html_body="",
                failed_at=now,
                last_error="recipient_refused",
            ),
        ]
    )
    await session.commit()

    report = await check_mail(
        _smtp_settings(),
        session_maker,
        now=now + timedelta(seconds=1),
        sender=ProbeSender(),
    )

    text = "\n".join(report.lines())
    assert not report.ok
    assert (report.sent, report.pending, report.failed) == (1, 0, 2)
    assert "smtp_553" in text and "вход в ящик — ok" in text
    # Опечатка в адресе — не поломка почты; адресов в выводе нет.
    assert text.count("не отправлено 1 (invite): smtp_553") == 1
    assert "recipient_refused" in text and "@acme" not in text


async def test_mail_check_flags_disabled_sending_and_stuck_queue(
    session: AsyncSession, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    now = datetime.now(UTC)
    session.add(
        OutboxEmail(
            to_email="anna@acme.ru",
            kind="login_code",
            subject="s",
            text_body="t",
            html_body="h",
            attempts=3,
            last_error="smtp_unavailable",
            created_at=now - timedelta(minutes=30),
        )
    )
    await session.commit()

    report = await check_mail(MailSettings(backend="console"), session_maker, now=now)

    lines = report.lines()
    assert lines[0].startswith("FAIL почта: console")
    assert any("отправка выключена" in line for line in lines)
    assert any("в очереди дольше 10 мин" in line and "30 мин" in line for line in lines)


@pytest.mark.parametrize(
    ("settings", "probe_error", "problem"),
    [
        (_smtp_settings(), MailDeliveryError("smtp_auth", retryable=False), "пароль"),
        (
            _smtp_settings(),
            MailDeliveryError("smtp_unavailable", retryable=True),
            "нет связи",
        ),
        (_smtp_settings(from_address="hello@krontoai.ru"), None, "553"),
        (_smtp_settings(smtp_password=None), None, "без входа"),
    ],
)
async def test_mail_check_explains_smtp_problems(
    session_maker: async_sessionmaker[AsyncSession],
    settings: MailSettings,
    probe_error: MailDeliveryError | None,
    problem: str,
) -> None:
    report = await check_mail(
        settings,
        session_maker,
        now=datetime.now(UTC),
        sender=ProbeSender(probe_error),
    )

    assert not report.ok
    assert any(problem in line for line in report.lines())


async def test_mail_check_is_ok_when_everything_goes_out(
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    report = await check_mail(
        _smtp_settings(), session_maker, now=datetime.now(UTC), sender=ProbeSender()
    )

    assert report.ok
    assert report.lines() == [
        "OK   почта: smtp smtp.example.ru:465 ssl, вход в ящик — ok; "
        "за 7 дн.: отправлено 0, ждут 0, не отправлено 0"
    ]
