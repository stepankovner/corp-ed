"""Уведомления нашей команде (решение владельца продукта 28.09, П-5).

Бот в Telegram: новая заявка на созвон, компания исчерпала пул, у
компании остановилось подключение. **Без персональных данных**: ни
имён, ни телефонов, ни почты, ни названий подключений, которые пишет
клиент, — только код компании, вид системы, числа и коды ошибок.
Подробности команда смотрит в CLI (`leads list`) и в журнале. Так
сообщения не становятся передачей персональных данных в иностранный
сервис.

Отправка — в фоне, с коротким таймаутом: недоступный Telegram не должен
задерживать ответ сотруднику или синхронизацию. Токен бота — в адресе
запроса, поэтому в журнал ошибок пишется только тип ошибки и код ответа.
"""

import asyncio
from datetime import date, datetime
from typing import Protocol

import httpx
import structlog

from corp_ed.core.config import TeamNotifySettings

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


class TelegramNotifier:
    def __init__(
        self,
        client: httpx.AsyncClient,
        token: str,
        chat_id: str,
        *,
        timeout: float = 5.0,
    ) -> None:
        self._client = client
        self._url = f"{TELEGRAM_API}/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._timeout = timeout
        self._pending: set[asyncio.Task[None]] = set()

    def notify(self, text: str) -> None:
        task = asyncio.get_running_loop().create_task(self._send(text))
        # Ссылка держит задачу до конца: иначе сборщик мусора может
        # прервать её на полпути.
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def drain(self) -> None:
        """Дождаться отправки (остановка процесса, тесты)."""
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)

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
            logger.warning("team_notify_failed", error=type(exc).__name__)
            return
        if response.status_code >= 400:
            logger.warning("team_notify_failed", status=response.status_code)


def build_team_notifier(
    client: httpx.AsyncClient, settings: TeamNotifySettings
) -> TeamNotifier:
    if settings.telegram_bot_token is None or settings.telegram_chat_id is None:
        return NULL_NOTIFIER
    return TelegramNotifier(
        client,
        settings.telegram_bot_token.get_secret_value(),
        settings.telegram_chat_id,
    )


async def drain(notifier: TeamNotifier) -> None:
    if isinstance(notifier, TelegramNotifier):
        await notifier.drain()


# --- тексты: только то, что можно отправить в чужой мессенджер ------------------


def lead_message(
    *, tariff: str, seats: int, preferred_date: date, preferred_slot: str
) -> str:
    return (
        f"Новая заявка на созвон: тариф {tariff}, {seats} мест, "
        f"удобно {preferred_date:%d.%m} {preferred_slot} МСК. "
        "Контакты — cli leads list."
    )


def pool_exhausted_message(
    *, company_code: str, used: int, pool: int, until: datetime
) -> str:
    return (
        f"Компания {company_code} исчерпала пул: {used} из {pool} кредитов. "
        f"Вопросы остановлены до {until:%d.%m}; места — cli set-seats."
    )


def connector_stopped_message(*, company_code: str, kind: str, code: str) -> str:
    return (
        f"Компания {company_code}: подключение {kind} остановлено, "
        f"ошибка {code}. Нужны новые учётные данные от админа компании."
    )


def company_request_message(*, seats: int | None) -> str:
    places = f", {seats} мест" if seats else ""
    return (
        f"Новая заявка на подключение компании{places}. "
        "Посмотреть и одобрить — cli requests list."
    )
