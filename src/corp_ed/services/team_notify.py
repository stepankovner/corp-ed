"""Уведомления нашей команде (решение владельца продукта 28.09, П-5).

Новая заявка на созвон, у компании кончились кредиты, заказ пакета
кредитов, у компании остановилось подключение. **Без персональных
данных**: ни имён, ни телефонов, ни почты, ни названий подключений,
которые пишет клиент, ни кода компании (он повторяет название, а у
клиента-ИП это фамилия) — только короткий id компании
(domain/company_ref.py; тот же показывает наша панель), вид системы,
числа и коды ошибок. Подробности команда смотрит в CLI (`leads list`) и
в журнале. Так сообщения не становятся передачей персональных данных в
иностранный сервис.

Каналы (TeamNotifySettings), можно оба:
- почта (TEAM_NOTIFY_EMAIL) — письмо в очередь outbox_emails, отправляет
  воркер с повторами (services/mail_worker.py). Основной канал: с сервера
  в России Telegram может быть недоступен (RISKS №50);
- бот в Telegram — запрос с коротким таймаутом. Токен бота — в адресе
  запроса, поэтому в журнал ошибок пишется только тип ошибки и код ответа.

Отправка — в фоне: недоступная почта или Telegram не должны задерживать
ответ сотруднику или синхронизацию.
"""

import asyncio
from collections.abc import Coroutine
from datetime import date, datetime
from typing import Any, Protocol
from uuid import UUID

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.config import TeamNotifySettings
from corp_ed.domain.company_ref import company_ref
from corp_ed.services.email_service import EmailService
from corp_ed.services.email_templates import team_notice

logger = structlog.get_logger()

TELEGRAM_API = "https://api.telegram.org"


class TeamNotifier(Protocol):
    def notify(self, text: str) -> None:
        """Отправить сообщение команде; не ждёт и не бросает исключений."""
        ...


class NullNotifier:
    """Канал не настроен: уведомлений нет (разработка, тесты)."""

    def notify(self, text: str) -> None:
        return None


NULL_NOTIFIER = NullNotifier()


class _Background:
    """Отправка фоновой задачей процесса; drain — дождаться всех."""

    def __init__(self) -> None:
        self._pending: set[asyncio.Task[None]] = set()

    def _spawn(self, send: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(send)
        # Ссылка держит задачу до конца: иначе сборщик мусора может
        # прервать её на полпути.
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def drain(self) -> None:
        """Дождаться отправки (остановка процесса, тесты)."""
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)


class TelegramNotifier(_Background):
    def __init__(
        self,
        client: httpx.AsyncClient,
        token: str,
        chat_id: str,
        *,
        timeout: float = 5.0,
    ) -> None:
        super().__init__()
        self._client = client
        self._url = f"{TELEGRAM_API}/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._timeout = timeout

    def notify(self, text: str) -> None:
        self._spawn(self._send(text))

    async def _send(self, text: str) -> None:
        try:
            response = await self._client.post(
                self._url,
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            # Не str(exc): в тексте ошибки httpx — адрес с токеном бота.
            logger.warning(
                "team_notify_failed", channel="telegram", error=type(exc).__name__
            )
            return
        if response.status_code >= 400:
            logger.warning(
                "team_notify_failed", channel="telegram", status=response.status_code
            )


class EmailNotifier(_Background):
    """Письмо в очередь — своей транзакцией: уведомление о событии, которое
    уже произошло, не должно зависеть от транзакции вызывающего."""

    def __init__(
        self, session_maker: async_sessionmaker[AsyncSession], to: str
    ) -> None:
        super().__init__()
        self._session_maker = session_maker
        self._to = to

    def notify(self, text: str) -> None:
        self._spawn(self._enqueue(text))

    async def _enqueue(self, text: str) -> None:
        try:
            async with self._session_maker() as session:
                EmailService(session).enqueue(self._to, team_notice(text))
                await session.commit()
        except Exception as exc:
            logger.warning(
                "team_notify_failed", channel="email", error=type(exc).__name__
            )


class FanoutNotifier:
    """Несколько каналов сразу: почта и Telegram."""

    def __init__(self, channels: list[TeamNotifier]) -> None:
        self.channels = channels

    def notify(self, text: str) -> None:
        for channel in self.channels:
            channel.notify(text)


def build_team_notifier(
    client: httpx.AsyncClient,
    settings: TeamNotifySettings,
    session_maker: async_sessionmaker[AsyncSession] | None = None,
) -> TeamNotifier:
    """session_maker нужен почте — сессии процесса (API, воркер, CLI)."""
    channels: list[TeamNotifier] = []
    if settings.email:
        if session_maker is None:
            raise ValueError("TEAM_NOTIFY_EMAIL needs a database session maker")
        channels.append(EmailNotifier(session_maker, settings.email))
    if settings.telegram_bot_token is not None and settings.telegram_chat_id:
        channels.append(
            TelegramNotifier(
                client,
                settings.telegram_bot_token.get_secret_value(),
                settings.telegram_chat_id,
            )
        )
    if not channels:
        return NULL_NOTIFIER
    if len(channels) == 1:
        return channels[0]
    return FanoutNotifier(channels)


async def drain(notifier: TeamNotifier) -> None:
    channels = notifier.channels if isinstance(notifier, FanoutNotifier) else [notifier]
    for channel in channels:
        if isinstance(channel, _Background):
            await channel.drain()


# --- тексты: только то, что можно отправить в чужой мессенджер ------------------------


def lead_message(
    *, tariff: str, seats: int, preferred_date: date, preferred_slot: str
) -> str:
    return (
        f"Новая заявка на созвон: тариф {tariff}, {seats} мест, "
        f"удобно {preferred_date:%d.%m} {preferred_slot} МСК. "
        "Контакты — cli leads list."
    )


def pool_exhausted_message(
    *, tenant_id: UUID, used: int, pool: int, until: datetime
) -> str:
    return (
        f"Компания {company_ref(tenant_id)} исчерпала пул: {used} из {pool} "
        f"кредитов, купленных нет. Вопросы остановлены до {until:%d.%m}; пакет "
        "кредитов — заказом администратора, места — в нашей панели."
    )


def credit_order_message(
    *, tenant_id: UUID, number: int, credits: int, amount_kopecks: int
) -> str:
    return (
        f"Компания {company_ref(tenant_id)}: заказ № {number} — "
        f"{_thousands(credits)} кредитов на {_thousands(amount_kopecks // 100)} ₽, "
        "ждёт оплаты по счёту. Отметить оплату — в нашей панели, «Кредиты»."
    )


def payment_review_message(
    *, amount_kopecks: int | None, problem: str, tenant_id: UUID | None
) -> str:
    """Платёж из банка не зачёлся сам — разобрать в панели. Без реквизитов
    и назначения: в чужой мессенджер — только сумма и код причины."""
    amount = (
        f"{_thousands(amount_kopecks // 100)} ₽" if amount_kopecks is not None else "?"
    )
    company = f" (компания {company_ref(tenant_id)})" if tenant_id else ""
    return (
        f"Платёж на {amount}{company} не зачтён автоматически: {problem}. "
        "Разобрать — в нашей панели, «Оплата»."
    )


def billing_overdue_message(*, tenant_id: UUID, paid_until: date) -> str:
    return (
        f"Компания {company_ref(tenant_id)}: подписка просрочена — оплачено до "
        f"{paid_until:%d.%m.%Y}, счёт на продление не оплачен. Блокировки нет; "
        "решение — в нашей панели."
    )


def seats_topup_failed_message(*, tenant_id: UUID) -> str:
    return (
        f"Компания {company_ref(tenant_id)}: места добавлены, но доплату выставить не "
        "удалось (банк не ответил или нет почты для чека). Выставьте счёт "
        "вручную."
    )


def _thousands(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def connector_stopped_message(*, tenant_id: UUID, kind: str, code: str) -> str:
    return (
        f"Компания {company_ref(tenant_id)}: подключение {kind} остановлено, "
        f"ошибка {code}. Нужны новые учётные данные от админа компании."
    )


def tariff_request_message(
    *,
    tenant_id: UUID,
    current: str,
    wanted: str,
    seats: tuple[int, int] | None,
    has_comment: bool,
) -> str:
    """Комментарий администратора сюда не идёт: его пишут свободным текстом
    (могут быть имена и телефоны), а Telegram — иностранный сервис
    (решение 10.10). Текст — в нашей панели, во «Обращениях»."""
    places = f", мест: {seats[0]} → {seats[1]}" if seats else ""
    note = " Есть комментарий — в нашей панели, «Обращения»." if has_comment else ""
    return (
        f"Компания {company_ref(tenant_id)} просит сменить тариф: "
        f"{current} → {wanted}{places}. "
        f"Ответить администратору и поменять — в нашей панели.{note}"
    )


def support_message(*, request_id: UUID, topic: str, tenant_id: UUID | None) -> str:
    company = company_ref(tenant_id) if tenant_id else "— (без компании)"
    return (
        f"Обращение в поддержку №{str(request_id)[:8]}: {topic}, "
        f"компания {company}. Текст и почта — в нашей панели, «Обращения»."
    )


def company_request_message(*, seats: int | None) -> str:
    places = f", {seats} мест" if seats else ""
    return (
        f"Новая заявка на подключение компании{places}. "
        "Посмотреть и одобрить — cli requests list."
    )
