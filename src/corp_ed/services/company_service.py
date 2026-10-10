"""Настройки компании в интерфейсе (ТЗ §7): название, логотип, режим
«ответа нет», правила второго фактора и «запомнить устройство», домены
почты, срок хранения диалогов, личный дневной лимит кредитов; заявка на
смену тарифа.

До этапа 7 всё это меняла команда через CLI. Теперь — администратор
компании; каждое изменение пишется в журнал действий с тем, что было и
что стало. Тариф и места по-прежнему задаёт команда: администратор
только оставляет заявку, она приходит команде в Telegram (П-5).
"""

import hashlib
import hmac
import re
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.config import get_settings
from corp_ed.core.exceptions import DomainError, NotFoundError
from corp_ed.core.tenant_context import require_tenant
from corp_ed.domain.models import (
    MemberStatus,
    SupportRequest,
    Tenant,
    TenantLogo,
    User,
)
from corp_ed.domain.tariffs import PLANS, Tariff, plan_for
from corp_ed.domain.types import NotFoundMode
from corp_ed.ingest.images import ImageKind
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.services.avatar_service import MAX_AVATAR_BYTES, reencode_image
from corp_ed.services.team_notify import (
    NULL_NOTIFIER,
    TeamNotifier,
    tariff_request_message,
)

logger = structlog.get_logger()

MAX_DOMAINS = 10
URL_LIFETIME_S = 86_400
_DAY = 86_400
_DOMAIN = re.compile(
    r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)


class InvalidLogoError(DomainError):
    """Файл — не картинка в JPEG, PNG или WebP либо слишком большой. HTTP 400."""

    code = "invalid_logo"

    def __init__(
        self, message: str = "Загрузите логотип в PNG, JPEG или WebP до 5 МБ"
    ) -> None:
        super().__init__(message)


class InvalidDomainError(DomainError):
    """Домен почты записан неверно. HTTP 400."""

    code = "invalid_domain"


def _invalid_logo(message: str | None) -> DomainError:
    return InvalidLogoError(message) if message else InvalidLogoError()


def normalize_domain(value: str) -> str:
    """«@Acme.ru», «acme.ru.», «https://acme.ru» → «acme.ru»; иначе ошибка."""
    domain = value.strip().casefold()
    domain = re.sub(r"^[a-z]+://", "", domain).lstrip("@").rstrip(".").split("/")[0]
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise InvalidDomainError(f"Не похоже на домен почты: {value.strip()}") from exc
    if not _DOMAIN.match(domain):
        raise InvalidDomainError(f"Не похоже на домен почты: {value.strip()}")
    return domain


def _signing_key() -> bytes:
    # Свой ключ: подпись ссылки на логотип не подходит к ссылкам на фото.
    secret = get_settings().secret_key.get_secret_value().encode()
    return hmac.new(secret, b"kronto:logo-url", hashlib.sha256).digest()


def _signature(tenant_id: UUID, version: str, expires: int) -> str:
    message = f"{tenant_id}:{version}:{expires}".encode()
    return hmac.new(_signing_key(), message, hashlib.sha256).hexdigest()[:32]


def logo_url(tenant_id: UUID, version: str, now: float | None = None) -> str:
    """Подписанная ссылка на логотип до конца следующих суток (UTC)."""
    moment = time.time() if now is None else now
    expires = (int(moment) // _DAY + 2) * _DAY
    sig = _signature(tenant_id, version, expires)
    return f"/api/v1/logos/{tenant_id}?v={version}&exp={expires}&sig={sig}"


def check_logo_signature(
    tenant_id: UUID, version: str, expires: int, sig: str, now: float | None = None
) -> bool:
    if expires < (time.time() if now is None else now):
        return False
    return hmac.compare_digest(_signature(tenant_id, version, expires), sig)


class _Unset:
    pass


UNSET = _Unset()


@dataclass(frozen=True)
class CompanySettings:
    tenant: Tenant
    logo_url: str | None
    members: int
    """Работающие люди компании — сколько мест занято."""


class CompanyService:
    def __init__(
        self,
        session: AsyncSession,
        audit: AuditRepository,
        notifier: TeamNotifier = NULL_NOTIFIER,
    ) -> None:
        self.session = session
        self.audit = audit
        self.notifier = notifier

    async def settings(self) -> CompanySettings:
        tenant = await self._tenant()
        members = await self.session.scalar(
            select(func.count()).where(
                User.tenant_id == tenant.id, User.status == MemberStatus.ACTIVE
            )
        )
        return CompanySettings(
            tenant=tenant,
            logo_url=await self.logo_url_for(tenant.id),
            members=int(members or 0),
        )

    async def update(
        self,
        actor: User,
        *,
        name: str | _Unset = UNSET,
        not_found_mode: NotFoundMode | _Unset = UNSET,
        mfa_policy: str | _Unset = UNSET,
        allow_remember_device: bool | _Unset = UNSET,
        email_domains: list[str] | _Unset = UNSET,
        chat_retention_months: int | _Unset = UNSET,
        daily_credits_per_member: int | None | _Unset = UNSET,
    ) -> CompanySettings:
        """Поменять настройки; в журнал — что было и что стало.

        mfa_policy=strong: всем сотрудникам — приложение или ключ доступа;
        без них данные компании закрыты, фронт ведёт на настройку защиты.
        allow_remember_device=False: галочка «запомнить» перестаёт
        действовать для людей этой компании при следующем входе.
        chat_retention_months: диалоги без активности дольше стольких
        месяцев удалит ближайший purge (варианты — CHAT_RETENTION_MONTHS,
        их проверяет схема ручки).
        daily_credits_per_member: личный дневной лимит кредитов (решение
        владельца 09.10); None — без лимита.
        """
        tenant = await self._tenant()
        changes: dict[str, dict[str, Any]] = {}

        def change(field: str, value: Any) -> None:
            old = getattr(tenant, field)
            if old != value:
                changes[field] = {"old": old, "new": value}
                setattr(tenant, field, value)

        if not isinstance(name, _Unset):
            cleaned = " ".join(name.split())[:200]
            if cleaned:
                change("name", cleaned)
        if not isinstance(not_found_mode, _Unset):
            change("not_found_mode", not_found_mode.value)
        if not isinstance(mfa_policy, _Unset):
            change("mfa_policy", mfa_policy)
        if not isinstance(allow_remember_device, _Unset):
            change("allow_remember_device", allow_remember_device)
        if not isinstance(email_domains, _Unset):
            domains = list(dict.fromkeys(normalize_domain(d) for d in email_domains))
            if len(domains) > MAX_DOMAINS:
                raise InvalidDomainError(f"Доменов — не больше {MAX_DOMAINS}")
            change("email_domains", domains)
        if not isinstance(chat_retention_months, _Unset):
            change("chat_retention_months", chat_retention_months)
        if not isinstance(daily_credits_per_member, _Unset):
            change("daily_credits_per_member", daily_credits_per_member)
        if changes:
            self.audit.record(
                AuditAction.TENANT_SETTINGS_UPDATED,
                tenant_id=tenant.id,
                actor_id=actor.id,
                target_type="tenant",
                target_id=tenant.id,
                details=changes,
            )
        await self.session.commit()
        return await self.settings()

    async def save_logo(self, actor: User, raw: bytes) -> str:
        if not raw or len(raw) > MAX_AVATAR_BYTES:
            raise InvalidLogoError()
        # Вписывается в квадрат 256×256 без обрезки (ingest/images.py).
        content = await reencode_image(ImageKind.LOGO, raw, _invalid_logo)
        version = hashlib.sha256(content).hexdigest()[:16]
        tenant_id = require_tenant()
        logo = await self.session.get(TenantLogo, tenant_id)
        if logo is None:
            self.session.add(
                TenantLogo(tenant_id=tenant_id, content=content, version=version)
            )
        else:
            logo.content = content
            logo.version = version
        self._audit_logo(actor, "set")
        await self.session.commit()
        return logo_url(tenant_id, version)

    async def remove_logo(self, actor: User) -> None:
        tenant_id = require_tenant()
        await self.session.execute(
            delete(TenantLogo).where(TenantLogo.tenant_id == tenant_id)
        )
        self._audit_logo(actor, "removed")
        await self.session.commit()

    async def load_logo(self, tenant_id: UUID, version: str) -> bytes | None:
        logo = await self.session.get(TenantLogo, tenant_id)
        if logo is None or logo.version != version:
            return None
        return logo.content

    async def logo_urls(self, tenant_ids: list[UUID]) -> dict[UUID, str]:
        """Ссылки на логотипы компаний человека — одним запросом."""
        if not tenant_ids:
            return {}
        rows = await self.session.execute(
            select(TenantLogo.tenant_id, TenantLogo.version).where(
                TenantLogo.tenant_id.in_(tenant_ids)
            )
        )
        return {row.tenant_id: logo_url(row.tenant_id, row.version) for row in rows}

    async def logo_url_for(self, tenant_id: UUID) -> str | None:
        return (await self.logo_urls([tenant_id])).get(tenant_id)

    async def request_tariff(
        self, actor: User, *, tariff: Tariff, seats: int | None, comment: str | None
    ) -> None:
        """«Сменить тариф» (ТЗ §7): заявка команде; меняет тариф команда
        (cli set-tariff, позже — наша панель), оплата — §10."""
        tenant = await self._tenant()
        current = plan_for(tenant.tariff)
        wanted = PLANS[tariff]
        comment = " ".join((comment or "").split())[:1000] or None
        self.audit.record(
            AuditAction.TENANT_TARIFF_CHANGE_REQUESTED,
            tenant_id=tenant.id,
            actor_id=actor.id,
            target_type="tenant",
            target_id=tenant.id,
            details={
                "from": tenant.tariff,
                "to": tariff.value,
                "seats": seats,
                "comment": comment,
            },
        )
        places = f", мест: {tenant.seats} → {seats}" if seats else ""
        if comment and actor.account_id is not None:
            # Комментарий — в «Обращения» нашей панели: там команда его
            # читает и отвечает администратору на почту; в Telegram — нет.
            self.session.add(
                SupportRequest(
                    account_id=actor.account_id,
                    tenant_id=tenant.id,
                    topic="billing",
                    message=(
                        f"Запрос смены тарифа: {current.title} → {wanted.title}"
                        f"{places}.\n\n{comment}"
                    ),
                )
            )
        await self.session.commit()
        self.notifier.notify(
            tariff_request_message(
                tenant_id=tenant.id,
                current=current.title,
                wanted=wanted.title,
                seats=(tenant.seats, seats) if seats else None,
                has_comment=comment is not None,
            )
        )
        logger.info(
            "tariff_change_requested", tenant_id=str(tenant.id), tariff=tariff.value
        )

    async def _tenant(self) -> Tenant:
        tenant = await self.session.get(Tenant, require_tenant())
        if tenant is None:
            raise NotFoundError("Компания не найдена")
        return tenant

    def _audit_logo(self, actor: User, what: str) -> None:
        self.audit.record(
            AuditAction.TENANT_LOGO_UPDATED,
            tenant_id=actor.tenant_id,
            actor_id=actor.id,
            target_type="tenant",
            target_id=actor.tenant_id,
            details={"logo": what},
        )
