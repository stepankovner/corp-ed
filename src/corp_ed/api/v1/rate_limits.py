"""Политики ограничения частоты для ручек API.

Числа — стартовые, подбираются по логам `rate_limited`. Главное в них
не точность, а порядок: человек не входит 30 раз за 15 минут и не
задаёт 30 вопросов в минуту, скрипт — легко.

Fail-closed или fail-open, когда Redis недоступен:
- вход, обновление токена, смена пароля — fail-closed (503): без
  лимита пароль перебирается, и лучше временно не пустить никого,
  чем пустить перебор;
- вопросы, поиск, загрузка — fail-open с ошибкой в логе: их стоимость
  ограничена ещё и пулом кредитов компании, а отказ в ответах всем
  сотрудникам из-за упавшего счётчика — непропорционально.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

import structlog
from fastapi import Depends, Request

from corp_ed.api.v1.dependencies import get_current_user
from corp_ed.core.exceptions import ServiceUnavailableError
from corp_ed.core.rate_limit import (
    RateLimitedError,
    RateLimiter,
    RateLimiterUnavailableError,
)
from corp_ed.domain.models import User

logger = structlog.get_logger()


@dataclass(frozen=True)
class RatePolicy:
    name: str
    limit: int
    window: int
    """Длина окна в секундах."""
    fail_open: bool


LOGIN_PER_IP = RatePolicy("login-ip", limit=30, window=900, fail_open=False)
# Считаются только НЕУДАЧНЫЕ попытки на почту, в том числе несуществующую:
# блокировка есть для любого адреса, поэтому по ней нельзя узнать,
# существует ли учётка.
LOGIN_FAILURES_PER_ACCOUNT = RatePolicy(
    "login-account", limit=10, window=900, fail_open=False
)
REFRESH_PER_IP = RatePolicy("refresh-ip", limit=60, window=60, fail_open=False)
# Приглашение: предпросмотр и вступление. Запас — на офис за одним NAT,
# где вся команда вступает в один час. Перебор токена ссылки бессмыслен
# (256 бит); код — 40 бит, и 60 попыток за 15 минут с адреса — миллионы
# лет на одно живое приглашение.
INVITE_PER_IP = RatePolicy("invite-ip", limit=60, window=900, fail_open=False)
# Регистрация и письма (ТЗ §3). Письма — ещё и против «почтовой бомбы» на
# чужой адрес и лимита ящика Яндекс 360 (300 писем в сутки).
REGISTER_PER_IP = RatePolicy("register-ip", limit=10, window=3600, fail_open=False)
MAIL_PER_ADDRESS = RatePolicy("mail-address", limit=5, window=3600, fail_open=False)
MAIL_PER_IP = RatePolicy("mail-ip", limit=20, window=3600, fail_open=False)
# Ввод кода из письма: у самого кода 5 попыток, лимит по IP — против
# перебора по многим адресам сразу.
VERIFY_PER_IP = RatePolicy("verify-ip", limit=30, window=900, fail_open=False)
EMAIL_CHANGE_PER_ACCOUNT = RatePolicy(
    "email-change", limit=10, window=3600, fail_open=False
)
# Фото профиля: перекодирование занимает CPU — не чаще пары раз в минуту.
AVATAR_PER_ACCOUNT = RatePolicy("avatar", limit=20, window=3600, fail_open=False)
# Отдача фото по подписанной ссылке: списки коллег грузят десятки сразу.
AVATAR_FETCH_PER_IP = RatePolicy("avatar-ip", limit=600, window=60, fail_open=True)
# Отделы и должности: правит человек или админ, сотни в час — уже не люди.
PEOPLE_EDIT_PER_TENANT = RatePolicy(
    "people-edit", limit=300, window=3600, fail_open=False
)
COMPANY_REQUEST_PER_ACCOUNT = RatePolicy(
    "company-request", limit=5, window=86400, fail_open=False
)
# Заявка на созвон со страницы тарифов: человек отправляет одну-две. Общий
# суточный потолок — против засорения базы персональными данными с многих
# адресов.
LEAD_PER_IP = RatePolicy("lead-ip", limit=5, window=3600, fail_open=False)
LEADS_PER_DAY = RatePolicy("lead-all", limit=300, window=86400, fail_open=False)
PASSWORD_CHANGE_PER_USER = RatePolicy(
    "password-user", limit=5, window=900, fail_open=False
)
# Остальные действия «подтвердите паролем» (второй фактор, резервные
# коды, удаление учётки): общий счётчик на учётку — перебрать текущий
# пароль украденным access-токеном через соседнюю ручку не выйдет.
PASSWORD_CONFIRM_PER_ACCOUNT = RatePolicy(
    "password-confirm", limit=10, window=900, fail_open=False
)
FAQ_PER_USER = RatePolicy("faq-user", limit=30, window=60, fail_open=True)
SEARCH_PER_USER = RatePolicy("search-user", limit=60, window=60, fail_open=True)
# Чат (ТЗ §6): вложения разбираются в песочнице и считают эмбеддинги;
# правки диалогов (переименовать, закрепить, оценить) — дешёвые.
ATTACHMENT_PER_USER = RatePolicy(
    "attachment-user", limit=30, window=3600, fail_open=True
)
# Просмотр диалога по общей ссылке: читают коллеги, перебор ссылок не
# нужен и бессмыслен (токен 192 бита), но и без лимита ручку не оставляем.
SHARED_VIEW_PER_USER = RatePolicy(
    "shared-view-user", limit=120, window=60, fail_open=True
)
CHAT_EDIT_PER_USER = RatePolicy(
    "chat-edit-user", limit=240, window=3600, fail_open=True
)
# Админка (ТЗ §7): настройки и папки — редкие правки; заявка на тариф
# уходит людям в Telegram — несколько в сутки.
COMPANY_EDIT_PER_TENANT = RatePolicy(
    "company-edit", limit=60, window=3600, fail_open=True
)
FOLDER_EDIT_PER_TENANT = RatePolicy(
    "folder-edit", limit=120, window=3600, fail_open=True
)
TARIFF_REQUEST_PER_TENANT = RatePolicy(
    "tariff-request", limit=5, window=86400, fail_open=False
)
# Наша панель (ТЗ §9): правки команды — сотни в час уже не люди, а
# украденная сессия; письма о новом пароле — не рассылка.
STAFF_EDIT_PER_ACCOUNT = RatePolicy(
    "staff-edit", limit=300, window=3600, fail_open=False
)
STAFF_RESET_PER_ACCOUNT = RatePolicy(
    "staff-reset", limit=20, window=3600, fail_open=False
)
# «Написать в поддержку»: человеку хватит пяти обращений в час, больше —
# уже рассылка в Telegram команды.
SUPPORT_PER_ACCOUNT = RatePolicy("support", limit=5, window=3600, fail_open=False)
# Песочница на сайте (ТЗ §1): вопрос без входа стоит вызова модели.
# Посетителю хватит десятка вопросов в час; общий суточный потолок — против
# раздачи модели всему интернету с многих адресов. Без Redis — отказ.
DEMO_PER_IP = RatePolicy("demo-ip", limit=10, window=3600, fail_open=False)
DEMO_PER_DAY = RatePolicy("demo-all", limit=300, window=86400, fail_open=False)
SUGGESTION_EDIT_PER_TENANT = RatePolicy(
    "suggestion-edit", limit=120, window=3600, fail_open=True
)
UPLOAD_PER_TENANT = RatePolicy("upload-tenant", limit=60, window=3600, fail_open=True)
# Создание и переиндексация материалов: каждый запрос — пачка платных
# эмбеддингов в воркере.
INGEST_PER_TENANT = RatePolicy("ingest-tenant", limit=120, window=3600, fail_open=True)
# Правки словаря — руками админа; сотни в час — уже скрипт.
GLOSSARY_PER_TENANT = RatePolicy(
    "glossary-tenant", limit=300, window=3600, fail_open=True
)
# Коннекторы: настройка — руками админа; «синхронизировать сейчас» и
# проверка учётных данных — запросы к системе клиента, их темп держим
# ниже её лимитов API.
CONNECTOR_WRITE_PER_TENANT = RatePolicy(
    "connector-tenant", limit=120, window=3600, fail_open=True
)
CONNECTOR_SYNC_PER_TENANT = RatePolicy(
    "connector-sync-tenant", limit=12, window=3600, fail_open=True
)
CONNECTOR_TEST_PER_TENANT = RatePolicy(
    "connector-test-tenant", limit=30, window=3600, fail_open=True
)
CONNECTOR_GRANT_PER_USER = RatePolicy(
    "connector-grant-user", limit=20, window=3600, fail_open=True
)
# Обратный вызов OAuth приходит без нашего токена — лимит по IP: подбор
# state или кода не должен быть бесплатным. Запас — на офис за одним NAT,
# где вся компания подключает источник в один час; злоупотребление и
# так ограничено подписью state и лимитом на /oauth/start по сотруднику.
CONNECTOR_OAUTH_CALLBACK_PER_IP = RatePolicy(
    "connector-oauth-ip", limit=300, window=900, fail_open=False
)


def get_rate_limiter(request: Request) -> RateLimiter:
    limiter: RateLimiter = request.app.state.rate_limiter
    return limiter


def client_ip(request: Request) -> str:
    """IP клиента.

    X-Forwarded-For здесь не читается: подделать его может любой. За
    reverse proxy адрес подставляет uvicorn (--proxy-headers и
    --forwarded-allow-ips с адресом прокси), и тогда request.client —
    уже настоящий клиент.
    """
    return request.client.host if request.client else "unknown"


async def enforce(limiter: RateLimiter, policy: RatePolicy, subject: str) -> None:
    try:
        decision = await limiter.hit(
            f"{policy.name}:{subject}", limit=policy.limit, window=policy.window
        )
    except RateLimiterUnavailableError:
        logger.error("rate_limiter_unavailable", policy=policy.name)
        if policy.fail_open:
            return
        raise ServiceUnavailableError() from None

    if not decision.allowed:
        logger.warning("rate_limited", policy=policy.name, count=decision.count)
        raise RateLimitedError(decision.retry_after)


async def ensure_not_locked(
    limiter: RateLimiter, policy: RatePolicy, subject: str
) -> None:
    """Отказать заранее, если неудачных попыток уже слишком много."""
    try:
        decision = await limiter.peek(f"{policy.name}:{subject}", limit=policy.limit)
    except RateLimiterUnavailableError:
        logger.error("rate_limiter_unavailable", policy=policy.name)
        if policy.fail_open:
            return
        raise ServiceUnavailableError() from None

    if not decision.allowed:
        logger.warning("account_locked", policy=policy.name)
        raise RateLimitedError(decision.retry_after)


async def record(limiter: RateLimiter, policy: RatePolicy, subject: str) -> None:
    """Засчитать событие, не отказывая сейчас.

    Для неудачного входа: ответ на эту попытку уже решён (401), и 429
    отсюда сделал бы его отличимым от остальных. Блокировку проверит
    ensure_not_locked на следующей попытке.
    """
    try:
        await limiter.hit(
            f"{policy.name}:{subject}", limit=policy.limit, window=policy.window
        )
    except RateLimiterUnavailableError:
        logger.error("rate_limiter_unavailable", policy=policy.name)


async def forget(limiter: RateLimiter, policy: RatePolicy, subject: str) -> None:
    try:
        await limiter.reset(f"{policy.name}:{subject}")
    except RateLimiterUnavailableError:
        logger.error("rate_limiter_unavailable", policy=policy.name)


def limit_by_ip(policy: RatePolicy) -> Callable[..., Awaitable[None]]:
    async def dependency(
        request: Request,
        limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    ) -> None:
        await enforce(limiter, policy, client_ip(request))

    return dependency


def limit_by_user(policy: RatePolicy) -> Callable[..., Awaitable[None]]:
    async def dependency(
        user: Annotated[User, Depends(get_current_user)],
        limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    ) -> None:
        await enforce(limiter, policy, str(user.id))

    return dependency


def limit_by_tenant(policy: RatePolicy) -> Callable[..., Awaitable[None]]:
    async def dependency(
        user: Annotated[User, Depends(get_current_user)],
        limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    ) -> None:
        await enforce(limiter, policy, str(user.tenant_id))

    return dependency
