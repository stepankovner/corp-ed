"""Уведомления команде в Telegram (П-5): отправка в фоне, сбои не ломают
работу, токен не попадает в логи, тексты без персональных данных."""

import json
from datetime import date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from corp_ed.core.config import TeamNotifySettings
from corp_ed.domain.models import Tenant, User
from corp_ed.services.team_notify import (
    NULL_NOTIFIER,
    TelegramNotifier,
    billing_overdue_message,
    build_team_notifier,
    connector_stopped_message,
    credit_order_message,
    lead_message,
    payment_review_message,
    pool_exhausted_message,
    seats_topup_failed_message,
)
from tests.conftest import make_credit_service
from tests.team_notify_helpers import RecordingNotifier
from tests.test_credits import spend

TOKEN = "123456:secret-bot-token"  # noqa: S105 — поддельный токен теста


async def test_telegram_message_is_sent_in_background() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        notifier = TelegramNotifier(client, TOKEN, "-100500")
        notifier.notify("Новая заявка")
        await notifier.drain()

    [request] = requests
    assert str(request.url) == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert json.loads(request.read()) == {
        "chat_id": "-100500",
        "text": "Новая заявка",
        "disable_web_page_preview": True,
    }


@pytest.mark.parametrize("failure", ["status", "network"])
async def test_failures_are_logged_without_token(failure: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "network":
            raise httpx.ConnectError(f"cannot reach {request.url}")
        return httpx.Response(502)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        notifier = TelegramNotifier(client, TOKEN, "-100500")
        with capture_logs() as logs:
            notifier.notify("текст")
            await notifier.drain()

    [entry] = [log for log in logs if log["event"] == "team_notify_failed"]
    assert TOKEN not in repr(entry)
    assert entry.get("status") == 502 or entry.get("error") == "ConnectError"


def test_settings_need_both_token_and_chat() -> None:
    with pytest.raises(ValidationError, match="TEAM_NOTIFY_TELEGRAM_CHAT_ID"):
        TeamNotifySettings(telegram_bot_token=TOKEN)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        TeamNotifySettings(telegram_chat_id="-100500")


async def test_not_configured_means_no_notifications() -> None:
    async with httpx.AsyncClient() as client:
        assert build_team_notifier(client, TeamNotifySettings()) is NULL_NOTIFIER
        configured = TeamNotifySettings(
            telegram_bot_token=TOKEN,  # type: ignore[arg-type]
            telegram_chat_id="-100500",
        )
        assert isinstance(build_team_notifier(client, configured), TelegramNotifier)


def test_messages_carry_no_personal_data() -> None:
    text = lead_message(
        tariff="base",
        seats=60,
        preferred_date=date(2026, 10, 1),
        preferred_slot="12:00–14:00",
    )
    assert text == (
        "Новая заявка на созвон: тариф base, 60 мест, удобно 01.10 12:00–14:00 МСК. "
        "Контакты — cli leads list."
    )
    until = datetime(2026, 10, 1, tzinfo=ZoneInfo("Europe/Moscow"))
    # Компания — первыми 8 символами id: код повторяет название, у ИП это
    # фамилия, а Telegram — иностранный сервис.
    tenant_id = UUID("1a2b3c4d-0000-4000-8000-000000000000")
    assert pool_exhausted_message(
        tenant_id=tenant_id, used=12600, pool=12600, until=until
    ) == (
        "Компания 1a2b3c4d исчерпала пул: 12600 из 12600 кредитов, купленных нет. "
        "Вопросы остановлены до 01.10; пакет кредитов — заказом администратора, "
        "места — в нашей панели."
    )
    assert credit_order_message(
        tenant_id=tenant_id, number=7, credits=2000, amount_kopecks=549_000
    ) == (
        "Компания 1a2b3c4d: заказ № 7 — 2 000 кредитов на 5 490 ₽, ждёт оплаты "
        "по счёту. Отметить оплату — в нашей панели, «Кредиты»."
    )
    assert connector_stopped_message(
        tenant_id=tenant_id, kind="yandex360", code="invalid_grant"
    ) == (
        "Компания 1a2b3c4d: подключение yandex360 остановлено, ошибка "
        "invalid_grant. Нужны новые учётные данные от админа компании."
    )
    assert payment_review_message(
        amount_kopecks=549_000, problem="amount_mismatch", tenant_id=tenant_id
    ) == (
        "Платёж на 5 490 ₽ (компания 1a2b3c4d) не зачтён автоматически: "
        "amount_mismatch. Разобрать — в нашей панели, «Оплата»."
    )
    assert "компания" not in payment_review_message(
        amount_kopecks=None, problem="unmatched", tenant_id=None
    )
    assert billing_overdue_message(
        tenant_id=tenant_id, paid_until=date(2026, 10, 31)
    ).startswith("Компания 1a2b3c4d: подписка просрочена — оплачено до 31.10.2026")
    assert seats_topup_failed_message(tenant_id=tenant_id).startswith(
        "Компания 1a2b3c4d: места добавлены"
    )


async def test_exhausted_pool_notifies_team_once(
    session: AsyncSession, tenant_ctx: Tenant, employee: User
) -> None:
    tenant_ctx.seats = 1
    await session.commit()
    sent: list[str] = []
    service = make_credit_service(session)
    service.notifier = RecordingNotifier(sent)

    # Как в FaqService: ответ в журнале (flush), потом отметка. Второй —
    # параллельный ответ на границе, который уже не остановить.
    for credits in (420, 1):
        usage = await service.usage()
        spend(session, employee, credits)
        await session.flush()
        await service.note_spend(usage, credits)
        await session.commit()

    assert len(sent) == 1
    assert sent[0].startswith(
        f"Компания {str(tenant_ctx.id)[:8]} исчерпала пул: 420 из 420 кредитов,"
    )
    assert "test" not in sent[0]
