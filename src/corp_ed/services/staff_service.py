"""Наша панель (ТЗ §9): команда kronto видит все компании.

Вместо команд на сервере: заявки на подключение компаний, компании (тариф,
места, срок пилота, приостановка), расход на модели, поиск человека для
помощи со входом. Изменения идут через те же сервисы, что и CLI
(TenantService, CompanyRequestService), — в журнал действий с учёткой
команды (current_staff, details.staff_account_id).

Чужие компании читаются так же, как в cli reindex: у каждой — своя сессия
и свой tenant_scope, RLS работает как в запросе пользователя. Учётки,
компании, заявки — вне RLS. Членства найденного человека — под правилом
own_membership (account_scope этой учётки).
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.database import ACCOUNT_SCOPE_OPTION
from corp_ed.core.exceptions import NotFoundError
from corp_ed.core.tenant_context import account_scope, tenant_scope
from corp_ed.domain.credits import billing_period
from corp_ed.domain.models import (
    Account,
    BackupCode,
    CompanyRequest,
    Connector,
    Material,
    MemberStatus,
    Passkey,
    QaLog,
    RefreshToken,
    StaffMember,
    Tenant,
    User,
    UserRole,
)
from corp_ed.repositories.audit_repository import AuditAction, AuditRepository

SEARCH_LIMIT = 20


@dataclass(frozen=True)
class CompanyRow:
    tenant: Tenant
    members: int
    pending: int
    """Ждут одобрения администратора."""
    admins: list[str]
    """Почта администраторов — кому писать по тарифу и пилоту."""
    credits_used: int
    """С начала расчётного месяца."""
    pool: int
    questions_month: int
    last_question_at: datetime | None
    documents: int
    connectors: int


@dataclass(frozen=True)
class SpendDay:
    day: date
    questions: int
    tokens: int
    credits: int


@dataclass(frozen=True)
class SpendModel:
    model: str
    questions: int
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class SpendCompany:
    tenant_id: UUID
    name: str
    company_code: str
    questions: int
    tokens: int
    credits: int


@dataclass(frozen=True)
class Spend:
    since: date
    until: date
    questions: int
    input_tokens: int
    output_tokens: int
    credits: int
    rub_per_1k_tokens: float | None
    days: list[SpendDay]
    models: list[SpendModel]
    companies: list[SpendCompany]

    @property
    def rub(self) -> float | None:
        if self.rub_per_1k_tokens is None:
            return None
        tokens = self.input_tokens + self.output_tokens
        return round(tokens / 1000 * self.rub_per_1k_tokens, 2)


@dataclass(frozen=True)
class PersonCompany:
    tenant_id: UUID
    company_name: str
    role: str
    status: str
    last_login_at: datetime | None


@dataclass(frozen=True)
class Person:
    account: Account
    companies: list[PersonCompany]
    totp: bool
    passkeys: int
    backup_codes: int
    """Неиспользованных резервных кодов."""
    sessions: int
    """Действующих входов (refresh-токенов)."""
    staff: bool


@dataclass(frozen=True)
class Overview:
    companies: int
    active_companies: int
    pilots_ending: int
    """Пилот заканчивается в ближайшие 7 дней или уже закончился."""
    requests_new: int
    accounts: int


class StaffService:
    def __init__(
        self,
        session: AsyncSession,
        session_maker: async_sessionmaker[AsyncSession],
        audit: AuditRepository,
        *,
        zone: ZoneInfo,
        credits_per_seat: int,
        rub_per_1k_tokens: float | None,
    ) -> None:
        self.session = session
        self.session_maker = session_maker
        self.audit = audit
        self.zone = zone
        self.credits_per_seat = credits_per_seat
        self.rub_per_1k_tokens = rub_per_1k_tokens

    async def is_staff(self, account_id: UUID) -> bool:
        return await self.session.get(StaffMember, account_id) is not None

    # --- обзор ---------------------------------------------------------------

    async def overview(self, now: datetime | None = None) -> Overview:
        today = (now or datetime.now(UTC)).astimezone(self.zone).date()
        tenants = (await self.session.scalars(select(Tenant))).all()
        requests_new = await self.session.scalar(
            select(func.count()).where(CompanyRequest.status == "new")
        )
        accounts = await self.session.scalar(select(func.count()).select_from(Account))
        return Overview(
            companies=len(tenants),
            active_companies=sum(1 for t in tenants if t.is_active),
            pilots_ending=sum(
                1
                for t in tenants
                if t.is_active
                and t.pilot_until is not None
                and t.pilot_until <= today + timedelta(days=7)
            ),
            requests_new=int(requests_new or 0),
            accounts=int(accounts or 0),
        )

    # --- компании ------------------------------------------------------------

    async def companies(self, now: datetime | None = None) -> list[CompanyRow]:
        tenants = (
            await self.session.scalars(select(Tenant).order_by(Tenant.name))
        ).all()
        start, _ = billing_period(now or datetime.now(UTC), self.zone)
        return [await self._company_row(tenant, start) for tenant in tenants]

    async def company(self, tenant_id: UUID, now: datetime | None = None) -> CompanyRow:
        tenant = await self._tenant(tenant_id)
        start, _ = billing_period(now or datetime.now(UTC), self.zone)
        return await self._company_row(tenant, start)

    async def set_pilot(self, tenant_id: UUID, until: date | None) -> Tenant:
        tenant = await self._tenant(tenant_id)
        previous = tenant.pilot_until
        if previous != until:
            tenant.pilot_until = until
            self.audit.record(
                AuditAction.TENANT_PILOT_CHANGED,
                tenant_id=tenant.id,
                target_type="tenant",
                target_id=tenant.id,
                details={
                    "from": previous.isoformat() if previous else None,
                    "to": until.isoformat() if until else None,
                },
            )
            await self.session.commit()
        return tenant

    async def _company_row(self, tenant: Tenant, month_start: datetime) -> CompanyRow:
        with tenant_scope(tenant.id):
            async with self.session_maker() as session:
                members = await session.execute(
                    select(User.status, func.count()).group_by(User.status)
                )
                by_status = {status: int(count) for status, count in members}
                admins = (
                    await session.scalars(
                        select(Account.email)
                        .join(User, User.account_id == Account.id)
                        .where(
                            User.role == UserRole.ADMIN,
                            User.status == MemberStatus.ACTIVE,
                        )
                        .order_by(Account.email)
                    )
                ).all()
                usage = (
                    await session.execute(
                        select(
                            func.coalesce(func.sum(QaLog.credits), 0),
                            func.count(),
                        ).where(QaLog.created_at >= month_start)
                    )
                ).one()
                last_question = await session.scalar(select(func.max(QaLog.created_at)))
                documents = await session.scalar(
                    select(func.count()).select_from(Material)
                )
                connectors = await session.scalar(
                    select(func.count()).select_from(Connector)
                )
        return CompanyRow(
            tenant=tenant,
            members=by_status.get(MemberStatus.ACTIVE, 0),
            pending=by_status.get(MemberStatus.PENDING, 0),
            admins=list(admins),
            credits_used=int(usage[0]),
            pool=tenant.seats * self.credits_per_seat,
            questions_month=int(usage[1]),
            last_question_at=last_question,
            documents=int(documents or 0),
            connectors=int(connectors or 0),
        )

    # --- расход --------------------------------------------------------------

    async def spend(self, days: int, now: datetime | None = None) -> Spend:
        """Расход на модель ответа за days дней по времени биллинга:
        токены из журнала ответов (qa_log), по дням, моделям и компаниям.
        Эмбеддинги в журнал не пишутся — здесь их нет."""
        local_now = (now or datetime.now(UTC)).astimezone(self.zone)
        first = local_now.date() - timedelta(days=days - 1)
        since = datetime(first.year, first.month, first.day, tzinfo=self.zone)
        tenants = (await self.session.scalars(select(Tenant))).all()

        per_day: dict[date, list[int]] = {}
        per_model: dict[str, list[int]] = {}
        companies: list[SpendCompany] = []
        local_day = cast(func.timezone(self.zone.key, QaLog.created_at), Date)
        tokens = QaLog.input_tokens + QaLog.output_tokens
        for tenant in tenants:
            with tenant_scope(tenant.id):
                async with self.session_maker() as session:
                    for row in await session.execute(
                        select(
                            local_day.label("day"),
                            func.count(),
                            func.coalesce(func.sum(tokens), 0),
                            func.coalesce(func.sum(QaLog.credits), 0),
                        )
                        .where(QaLog.created_at >= since)
                        .group_by(local_day)
                    ):
                        bucket = per_day.setdefault(row[0], [0, 0, 0])
                        bucket[0] += int(row[1])
                        bucket[1] += int(row[2])
                        bucket[2] += int(row[3])
                    for model_row in await session.execute(
                        select(
                            func.coalesce(QaLog.llm_model, "—"),
                            func.count(),
                            func.coalesce(func.sum(QaLog.input_tokens), 0),
                            func.coalesce(func.sum(QaLog.output_tokens), 0),
                        )
                        .where(QaLog.created_at >= since)
                        .group_by(func.coalesce(QaLog.llm_model, "—"))
                    ):
                        model = per_model.setdefault(str(model_row[0]), [0, 0, 0])
                        model[0] += int(model_row[1])
                        model[1] += int(model_row[2])
                        model[2] += int(model_row[3])
                    total = (
                        await session.execute(
                            select(
                                func.count(),
                                func.coalesce(func.sum(tokens), 0),
                                func.coalesce(func.sum(QaLog.credits), 0),
                            ).where(QaLog.created_at >= since)
                        )
                    ).one()
            if total[0]:
                companies.append(
                    SpendCompany(
                        tenant_id=tenant.id,
                        name=tenant.name,
                        company_code=tenant.company_code,
                        questions=int(total[0]),
                        tokens=int(total[1]),
                        credits=int(total[2]),
                    )
                )

        days_series = [
            SpendDay(
                day=day,
                questions=per_day.get(day, [0, 0, 0])[0],
                tokens=per_day.get(day, [0, 0, 0])[1],
                credits=per_day.get(day, [0, 0, 0])[2],
            )
            for day in (first + timedelta(days=offset) for offset in range(days))
        ]
        models = sorted(
            (
                SpendModel(
                    model=name, questions=v[0], input_tokens=v[1], output_tokens=v[2]
                )
                for name, v in per_model.items()
            ),
            key=lambda m: m.input_tokens + m.output_tokens,
            reverse=True,
        )
        return Spend(
            since=first,
            until=local_now.date(),
            questions=sum(m.questions for m in models),
            input_tokens=sum(m.input_tokens for m in models),
            output_tokens=sum(m.output_tokens for m in models),
            credits=sum(d.credits for d in days_series),
            rub_per_1k_tokens=self.rub_per_1k_tokens,
            days=days_series,
            models=models,
            companies=sorted(companies, key=lambda c: c.tokens, reverse=True),
        )

    # --- люди ----------------------------------------------------------------

    async def search_people(self, query: str) -> list[Person]:
        """Учётки по почте или имени — помочь со входом. Не меньше трёх
        символов: перечислять всех людей kronto панели незачем."""
        needle = query.strip().casefold()
        if len(needle) < 3:
            return []
        pattern = f"%{_escape_like(needle)}%"
        accounts = (
            await self.session.scalars(
                select(Account)
                .where(
                    func.lower(Account.email).like(pattern, escape="\\")
                    | func.lower(
                        func.concat_ws(" ", Account.last_name, Account.first_name)
                    ).like(pattern, escape="\\")
                    | func.lower(
                        func.concat_ws(" ", Account.first_name, Account.last_name)
                    ).like(pattern, escape="\\")
                )
                .order_by(Account.email)
                .limit(SEARCH_LIMIT)
            )
        ).all()
        return [await self._person(account) for account in accounts]

    async def person(self, account_id: UUID) -> Person:
        account = await self.session.get(Account, account_id)
        if account is None:
            raise NotFoundError("Учётка не найдена")
        return await self._person(account)

    async def _person(self, account: Account) -> Person:
        with account_scope(account.id):
            async with self.session_maker() as session:
                rows = await session.execute(
                    select(
                        User.tenant_id,
                        Tenant.name,
                        User.role,
                        User.status,
                        User.last_login_at,
                    )
                    .join(Tenant, Tenant.id == User.tenant_id)
                    .where(User.account_id == account.id)
                    .execution_options(**{ACCOUNT_SCOPE_OPTION: True})
                    .order_by(Tenant.name)
                )
                companies = [
                    PersonCompany(
                        tenant_id=row[0],
                        company_name=row[1],
                        role=_value(row[2]),
                        status=_value(row[3]),
                        last_login_at=row[4],
                    )
                    for row in rows
                ]
        passkeys = await self.session.scalar(
            select(func.count()).where(Passkey.account_id == account.id)
        )
        backup = await self.session.scalar(
            select(func.count()).where(
                BackupCode.account_id == account.id, BackupCode.used_at.is_(None)
            )
        )
        sessions = await self.session.scalar(
            select(func.count()).where(
                RefreshToken.account_id == account.id,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.used_at.is_(None),
                RefreshToken.expires_at > datetime.now(UTC),
            )
        )
        return Person(
            account=account,
            companies=companies,
            totp=account.totp_enabled_at is not None,
            passkeys=int(passkeys or 0),
            backup_codes=int(backup or 0),
            sessions=int(sessions or 0),
            staff=await self.is_staff(account.id),
        )

    async def _tenant(self, tenant_id: UUID) -> Tenant:
        tenant = await self.session.get(Tenant, tenant_id)
        if tenant is None:
            raise NotFoundError("Компания не найдена")
        return tenant


def _value(item: Any) -> str:
    return str(getattr(item, "value", item))


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
