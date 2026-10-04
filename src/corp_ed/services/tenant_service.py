import re
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.exceptions import ConflictError, DomainError
from corp_ed.core.password_policy import validate_password
from corp_ed.core.security import hash_password
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.domain.models import Account, MemberStatus, Tenant, User, UserRole
from corp_ed.domain.tariffs import DEFAULT_TARIFF, Tariff, plan_for
from corp_ed.domain.types import DEFAULT_NOT_FOUND_MODE, NotFoundMode
from corp_ed.repositories.account_repository import AccountRepository
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository
from corp_ed.repositories.connector_repository import ConnectorRepository
from corp_ed.repositories.refresh_token_repository import RefreshTokenRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services.passwords import set_password

logger = structlog.get_logger()

# Код компании вводится при входе и попадает в логи: только латиница в
# нижнем регистре, цифры и дефис — без пробелов, юникода и спецсимволов.
_COMPANY_CODE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")

# Верхняя граница ловит опечатку в CLI (лишний ноль — десятикратный
# пул и счёт). Сегмент по досье — 30–300 сотрудников.
MAX_SEATS = 10_000


# Та же проверка, что у входа (LoginRequest.email): почту, которую
# отвергнет вход, заводить нельзя — администратор не смог бы войти.
_EMAIL = TypeAdapter(EmailStr)


@dataclass(frozen=True)
class TariffChange:
    tenant: Tenant
    connectors: int
    """Сколько подключений у компании сейчас — для предупреждения, если
    их больше, чем даёт новый тариф."""

    @property
    def over_tariff(self) -> bool:
        limit = plan_for(self.tenant.tariff).max_connectors
        return limit is not None and self.connectors > limit


class InvalidAdminEmailError(DomainError):
    def __init__(self) -> None:
        super().__init__(
            "Почта администратора не принимается входом: нужен настоящий "
            "адрес (зоны .test, .local, .example зарезервированы)"
        )


class InvalidCompanyCodeError(DomainError):
    def __init__(self) -> None:
        super().__init__(
            "Код компании: 2–63 символа, латиница в нижнем регистре, цифры, дефис"
        )


class InvalidSeatsError(DomainError):
    def __init__(self) -> None:
        super().__init__(f"Число мест: от 1 до {MAX_SEATS}")


@dataclass(frozen=True)
class ProvisionedTenant:
    tenant: Tenant
    admin: User
    account: Account
    account_created: bool
    """Учётки с этой почтой не было — заведена с временным паролем."""


class TenantService:
    """Заведение компаний. Только для команды Kronto, только из CLI.

    По досье (10.1) компанию подключает команда после созвона, а не
    клиент сам. Поэтому HTTP-ручки для этого нет вовсе: нельзя
    атаковать то, чего нет в сети. См. corp_ed.cli.
    """

    def __init__(
        self,
        tenant_repo: TenantRepository,
        user_repo: UserRepository,
        audit: AuditRepository,
        session: AsyncSession,
    ) -> None:
        self.tenant_repo = tenant_repo
        self.user_repo = user_repo
        self.audit = audit
        self.session = session

    async def provision(
        self,
        *,
        company_code: str,
        name: str,
        admin_email: str,
        admin_full_name: str | None,
        admin_password: str | None,
        seats: int,
        not_found_mode: NotFoundMode = DEFAULT_NOT_FOUND_MODE,
        tariff: Tariff = DEFAULT_TARIFF,
        commit: bool = True,
    ) -> ProvisionedTenant:
        """Создать компанию и сделать учётку её администратором — одной
        транзакцией.

        Учётка с admin_email уже есть (человек зарегистрировался сам,
        одобрение заявки) — она становится администратором, пароль не
        нужен. Нет — заводится с временным паролем оператора
        (admin_password, RISKS №42: программа его не генерирует), почта
        считается подтверждённой (её назвал клиент на созвоне), пароль
        потребуют сменить при первом входе.
        seats — оплаченные места: от них считается пул кредитов.
        not_found_mode — что отвечать, когда в документах ответа нет;
        по умолчанию общий ответ с пометкой (DEFAULT_NOT_FOUND_MODE,
        решение 29.09, BH-29).
        tariff — тариф компании (domain/tariffs.py, решение 30.09).
        """
        code = company_code.strip().casefold()
        if not _COMPANY_CODE.fullmatch(code):
            raise InvalidCompanyCodeError()
        try:
            email = _EMAIL.validate_python(admin_email.strip()).casefold()
        except ValidationError as exc:
            raise InvalidAdminEmailError() from exc
        _check_seats(seats)
        if await self.tenant_repo.get_by_company_code(code) is not None:
            raise ConflictError(f"Компания с кодом '{code}' уже существует")

        accounts = AccountRepository(self.session)
        account = await accounts.get_by_email(email)
        created = account is None
        if account is None:
            if admin_password is None:
                raise ConflictError(
                    f"Учётки {email} нет — нужен временный пароль администратора"
                )
            validate_password(admin_password, email=email)
            first, _, last = (admin_full_name or "").strip().partition(" ")
            account = await accounts.add(
                Account(
                    email=email,
                    hashed_password=hash_password(admin_password),
                    first_name=first or None,
                    last_name=last.strip() or None,
                    email_verified_at=datetime.now(UTC),
                    must_change_password=True,
                )
            )

        tenant = await self.tenant_repo.create(
            Tenant(
                company_code=code,
                name=name,
                seats=seats,
                not_found_mode=not_found_mode.value,
                tariff=tariff.value,
            )
        )
        with tenant_scope(tenant.id):
            admin = await self.user_repo.create(
                User(
                    account_id=account.id,
                    role=UserRole.ADMIN,
                    status=MemberStatus.ACTIVE,
                )
            )
            # actor_id пуст: действие выполнено из CLI на сервере, а не
            # пользователем системы. Это и есть отметка «сделала команда».
            self.audit.record(
                AuditAction.TENANT_CREATED,
                tenant_id=tenant.id,
                target_type="tenant",
                target_id=tenant.id,
                details={
                    "company_code": code,
                    "admin_user_id": str(admin.id),
                    "account_id": str(account.id),
                    "seats": seats,
                    "not_found_mode": not_found_mode.value,
                    "tariff": tariff.value,
                },
            )
            if commit:
                await self.session.commit()

        logger.info("tenant_provisioned", tenant_id=str(tenant.id), company_code=code)
        return ProvisionedTenant(
            tenant=tenant, admin=admin, account=account, account_created=created
        )

    async def set_active(self, company_code: str, *, active: bool) -> Tenant:
        """Приостановить или вернуть компанию.

        Токены пользователей приостановленной компании перестают
        приниматься сразу: get_current_user проверяет статус тенанта
        на каждый запрос.
        """
        tenant = await self.tenant_repo.get_by_company_code(company_code)
        if tenant is None:
            raise ConflictError(f"Компании с кодом '{company_code}' нет")
        tenant.is_active = active
        self.audit.record(
            AuditAction.TENANT_RESUMED if active else AuditAction.TENANT_SUSPENDED,
            tenant_id=tenant.id,
            target_type="tenant",
            target_id=tenant.id,
        )
        await self.session.commit()
        logger.info("tenant_status_changed", tenant_id=str(tenant.id), active=active)
        return tenant

    async def set_seats(self, company_code: str, seats: int) -> Tenant:
        """Изменить число оплаченных мест.

        Пул текущего месяца пересчитывается сразу: места × кредитов на
        место, без пропорции по дням. Докупили места в середине месяца —
        пул вырос сегодня; сократили — уменьшился, и если потрачено уже
        больше, обращения остановятся до следующего месяца.
        """
        _check_seats(seats)
        tenant = await self.tenant_repo.get_by_company_code(company_code)
        if tenant is None:
            raise ConflictError(f"Компании с кодом '{company_code}' нет")
        previous = tenant.seats
        tenant.seats = seats
        self.audit.record(
            AuditAction.TENANT_SEATS_CHANGED,
            tenant_id=tenant.id,
            target_type="tenant",
            target_id=tenant.id,
            details={"from": previous, "to": seats},
        )
        await self.session.commit()
        logger.info(
            "tenant_seats_changed",
            tenant_id=str(tenant.id),
            previous=previous,
            seats=seats,
        )
        return tenant

    async def set_tariff(
        self,
        company_code: str,
        tariff: Tariff,
        *,
        connector_limit: int | None = None,
        default_connector_limit: bool = False,
    ) -> TariffChange:
        """Сменить тариф и, если нужно, технический потолок подключений.

        connector_limit — поднять (или опустить) потолок для этой компании;
        default_connector_limit — вернуть общий CONNECTOR_MAX_PER_TENANT.
        Уже заведённые подключения не удаляются, даже если их больше, чем
        даёт новый тариф: новых просто нельзя добавить (CLI предупредит).
        """
        if connector_limit is not None and connector_limit <= 0:
            raise ConflictError("Потолок подключений — положительное число")
        tenant = await self.tenant_repo.get_by_company_code(company_code)
        if tenant is None:
            raise ConflictError(f"Компании с кодом '{company_code}' нет")
        previous = _tariff_state(tenant)
        tenant.tariff = tariff.value
        if default_connector_limit:
            tenant.connector_limit = None
        elif connector_limit is not None:
            tenant.connector_limit = connector_limit
        self.audit.record(
            AuditAction.TENANT_TARIFF_CHANGED,
            tenant_id=tenant.id,
            target_type="tenant",
            target_id=tenant.id,
            details={"from": previous, "to": _tariff_state(tenant)},
        )
        with tenant_scope(tenant.id):
            connectors = await ConnectorRepository(self.session).count()
        await self.session.commit()
        logger.info(
            "tenant_tariff_changed",
            tenant_id=str(tenant.id),
            tariff=tenant.tariff,
            connector_limit=tenant.connector_limit,
        )
        return TariffChange(tenant=tenant, connectors=connectors)

    async def set_not_found_mode(self, company_code: str, mode: NotFoundMode) -> Tenant:
        """Переключить ответ «в документах ответа нет» для компании.

        Действует со следующего вопроса. Уже данные ответы и их origin в
        журнале не меняются.
        """
        tenant = await self.tenant_repo.get_by_company_code(company_code)
        if tenant is None:
            raise ConflictError(f"Компании с кодом '{company_code}' нет")
        previous = tenant.not_found_mode
        tenant.not_found_mode = mode.value
        self.audit.record(
            AuditAction.TENANT_NOT_FOUND_MODE_CHANGED,
            tenant_id=tenant.id,
            target_type="tenant",
            target_id=tenant.id,
            details={"from": previous, "to": mode.value},
        )
        await self.session.commit()
        logger.info(
            "tenant_not_found_mode_changed", tenant_id=str(tenant.id), mode=mode.value
        )
        return tenant

    async def reset_password(self, email: str, temporary_password: str) -> Account:
        """Временный пароль учётки — из CLI на сервере.

        Обычно пароль восстанавливают по почте (ТЗ §3). Этот путь — для
        поддержки, когда письма не доходят. Пароль, как у create-tenant,
        придумывает оператор (RISKS №42); при входе его потребуют
        сменить, все сессии закрываются.
        """
        account = await AccountRepository(self.session).get_by_email(email)
        if account is None:
            raise ConflictError(f"Учётки с почтой {email.strip()} нет")
        validate_password(temporary_password, email=account.email)
        set_password(account, temporary_password)
        account.must_change_password = True
        if account.email_verified_at is None:
            account.email_verified_at = datetime.now(UTC)
        account.token_version += 1
        await RefreshTokenRepository(self.session).revoke_account(
            account.id, datetime.now(UTC)
        )
        # actor_id пуст — как у create-tenant: сделала команда из CLI.
        self.audit.record(
            AuditAction.USER_PASSWORD_RESET,
            details={"source": "cli", "account_id": str(account.id)},
        )
        await self.session.commit()
        logger.info("password_reset_by_operator", account_id=str(account.id))
        return account


def _tariff_state(tenant: Tenant) -> dict[str, str | int | None]:
    return {"tariff": tenant.tariff, "connector_limit": tenant.connector_limit}


def _check_seats(seats: int) -> None:
    if not 1 <= seats <= MAX_SEATS:
        raise InvalidSeatsError()
