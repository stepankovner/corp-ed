"""Песочница на сайте (ТЗ §1): вопрос без входа — ответ kronto по
документам вымышленной компании (corp_ed/demo).

Песочница — обычная компания в базе со своими документами, пулом
кредитов и журналом вопросов; спрашивает от имени её единственного
участника. Отличия от чата приложения:

- без входа и без истории: каждый вопрос — сам по себе, диалог нигде не
  хранится (в журнале вопросов — как у любой компании, с маской ПДн);
- режим STRICT: ответ только по документам, иначе честный отказ — иначе
  песочница стала бы бесплатным чатом с моделью для всего интернета;
- лимиты по IP и общий суточный, при недоступном Redis — отказ
  (api/v1/endpoints/demo.py); месячный потолок — пул кредитов компании.

Войти в учётку песочницы нельзя: вместо хеша пароля — заглушка, её не
примет ни один пароль (core/security.verify_password), а письма ей
выключены настройками уведомлений.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from corp_ed.core.config import DemoSettings
from corp_ed.core.exceptions import (
    CreditsExhaustedError,
    DailyLimitExhaustedError,
    DemoUnavailableError,
    DomainError,
)
from corp_ed.core.tenant_context import tenant_scope
from corp_ed.demo import COMPANY_NAME, SUGGESTED_QUESTIONS, TENANT_NAME, documents
from corp_ed.domain.models import (
    Account,
    Material,
    MaterialStatus,
    MemberStatus,
    NotificationSetting,
    Tenant,
    User,
    UserRole,
)
from corp_ed.domain.tariffs import Tariff
from corp_ed.domain.types import AnswerOrigin, ChunkMatch, NotFoundMode
from corp_ed.llm.errors import LLMError
from corp_ed.repositories.account_repository import AccountRepository
from corp_ed.repositories.ingest_job_repository import IngestJobRepository
from corp_ed.repositories.tenant_repository import TenantRepository
from corp_ed.repositories.user_repository import UserRepository
from corp_ed.services.faq_service import AnswerSink, FaqService

logger = structlog.get_logger()

NO_LOGIN_HASH = "!demo-sandbox-no-login"
"""Не хеш argon2: verify_password вернёт False на любой пароль."""

SOURCE_EXCERPT_CHARS = 600


@dataclass(frozen=True)
class DemoInfo:
    company: str
    documents: list[str]
    questions: list[str]


@dataclass(frozen=True)
class DemoSource:
    title: str
    heading_path: list[str]
    content: str


@dataclass(frozen=True)
class DemoAnswer:
    content: str
    origin: AnswerOrigin
    sources: list[DemoSource]


@dataclass(frozen=True)
class DemoSetupReport:
    tenant_created: bool
    created: int
    updated: int
    unchanged: int


class DemoService:
    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        settings: DemoSettings,
        build_faq: Callable[[AsyncSession], FaqService] | None = None,
    ) -> None:
        self.session_maker = session_maker
        self.settings = settings
        self.build_faq = build_faq

    # --- сайт -----------------------------------------------------------------

    async def info(self) -> DemoInfo:
        tenant = await self._tenant()
        if tenant is None:
            raise _off()
        return DemoInfo(
            company=COMPANY_NAME,
            documents=[document.title for document in documents()],
            questions=list(SUGGESTED_QUESTIONS),
        )

    async def ask(self, question: str, *, sink: AnswerSink | None = None) -> DemoAnswer:
        """Ответ без истории. sink — ход ответа для потока на сайте: текст
        печатается по мере генерации, итог — в возвращённом ответе."""
        if self.build_faq is None:
            raise RuntimeError("DemoService.ask needs build_faq")
        tenant = await self._tenant()
        if tenant is None:
            raise _off()
        with tenant_scope(tenant.id):
            async with self.session_maker() as session:
                member = await self._member(session)
                if member is None:
                    raise _off()
                if await _has_other_members(session, tenant.id, member.account_id):
                    # DEMO_COMPANY_CODE указывает на настоящую компанию:
                    # анонимно отвечать от её имени нельзя.
                    logger.error("demo_tenant_not_sandbox", tenant_id=str(tenant.id))
                    raise _off()
                faq = self.build_faq(session)
                try:
                    result = await faq.answer_turn(
                        question, member, history=[], sink=sink
                    )
                except (CreditsExhaustedError, DailyLimitExhaustedError):
                    logger.warning("demo_pool_exhausted")
                    raise DemoUnavailableError(
                        "demo_busy",
                        "Песочница на этот месяц исчерпала кредиты. Покажем kronto "
                        "на ваших документах — запишитесь на созвон.",
                    ) from None
                except LLMError as exc:
                    logger.warning("demo_llm_failed", error=str(exc))
                    raise DemoUnavailableError(
                        "demo_busy",
                        "Модель сейчас не отвечает. Попробуйте ещё раз через минуту.",
                    ) from None
        logger.info("demo_answered", origin=result.origin.value)
        return DemoAnswer(
            content=result.content,
            origin=result.origin,
            sources=[_source(match) for match in result.sources],
        )

    async def _tenant(self) -> Tenant | None:
        if not self.settings.enabled:
            return None
        async with self.session_maker() as session:
            tenant = await TenantRepository(session).get_by_company_code(
                self.settings.company_code
            )
        if tenant is None or not tenant.is_active:
            return None
        return tenant

    async def _member(self, session: AsyncSession) -> User | None:
        account = await AccountRepository(session).get_by_email(
            self.settings.account_email
        )
        if account is None:
            return None
        return await UserRepository(session).get_by_account(account.id)

    # --- выкатка: cli demo setup ----------------------------------------------

    async def setup(self) -> DemoSetupReport:
        """Завести компанию песочницы или привести её документы к
        corp_ed/demo. Повторный запуск ничего не меняет; изменённый текст
        документа — новая индексация только этого документа."""
        settings = self.settings
        async with self.session_maker() as session:
            tenants = TenantRepository(session)
            tenant = await tenants.get_by_company_code(settings.company_code)
            tenant_created = tenant is None
            if tenant is None:
                tenant = await tenants.create(
                    Tenant(
                        company_code=settings.company_code,
                        name=TENANT_NAME,
                        seats=settings.seats,
                        not_found_mode=NotFoundMode.STRICT.value,
                        tariff=Tariff.BASE.value,
                    )
                )
            else:
                # Пул — от мест: настройка DEMO_SEATS действует и на заведённую.
                tenant.seats = settings.seats
                tenant.not_found_mode = NotFoundMode.STRICT.value

            accounts = AccountRepository(session)
            account = await accounts.get_by_email(settings.account_email)
            if account is None:
                account = await accounts.add(
                    Account(
                        email=settings.account_email,
                        hashed_password=NO_LOGIN_HASH,
                        first_name="Песочница",
                        last_name="сайта",
                        email_verified_at=datetime.now(UTC),
                    )
                )
            else:
                account.hashed_password = NO_LOGIN_HASH

            with tenant_scope(tenant.id):
                if not tenant_created and await _has_other_members(
                    session, tenant.id, account.id
                ):
                    raise DomainError(
                        f"Компания {settings.company_code} уже есть и это не "
                        "песочница: в ней есть сотрудники. Проверьте "
                        "DEMO_COMPANY_CODE."
                    )
                users = UserRepository(session)
                member = await users.get_by_account(account.id)
                if member is None:
                    member = await users.create(
                        User(
                            account_id=account.id,
                            role=UserRole.ADMIN,
                            status=MemberStatus.ACTIVE,
                        )
                    )
                # Писем учётке песочницы не нужно: сводки, пул, подключения.
                prefs = await session.get(NotificationSetting, member.id)
                if prefs is None:
                    session.add(
                        NotificationSetting(
                            user_id=member.id,
                            email_connectors=False,
                            email_credits=False,
                            email_join_requests=False,
                            email_weekly_digest=False,
                        )
                    )
                created, updated, unchanged = await self._sync_documents(
                    session, tenant
                )
            await session.commit()
        logger.info(
            "demo_setup",
            tenant_created=tenant_created,
            created=created,
            updated=updated,
            unchanged=unchanged,
        )
        return DemoSetupReport(
            tenant_created=tenant_created,
            created=created,
            updated=updated,
            unchanged=unchanged,
        )

    @staticmethod
    async def _sync_documents(
        session: AsyncSession, tenant: Tenant
    ) -> tuple[int, int, int]:
        jobs = IngestJobRepository(session)
        existing = {
            material.title: material
            for material in (
                await session.scalars(
                    select(Material).where(Material.connector_id.is_(None))
                )
            ).all()
        }
        created = updated = unchanged = 0
        for document in documents():
            material = existing.get(document.title)
            if material is None:
                material = Material(title=document.title, content=document.content)
                session.add(material)
                await session.flush()
                await jobs.enqueue(tenant.id, material.id)
                created += 1
            elif material.content != document.content:
                material.content = document.content
                material.status = MaterialStatus.PENDING
                material.status_error = None
                await jobs.enqueue(tenant.id, material.id)
                updated += 1
            else:
                unchanged += 1
        return created, updated, unchanged


def _off() -> DemoUnavailableError:
    return DemoUnavailableError(
        "demo_off", "Песочница сейчас недоступна. Попробуйте позже."
    )


def _source(match: ChunkMatch) -> DemoSource:
    content = match.content
    if len(content) > SOURCE_EXCERPT_CHARS:
        content = content[:SOURCE_EXCERPT_CHARS].rsplit(" ", 1)[0] + "…"
    return DemoSource(
        title=match.title, heading_path=list(match.heading_path), content=content
    )


async def _has_other_members(
    session: AsyncSession, tenant_id: UUID, demo_account_id: UUID | None
) -> bool:
    """В компании песочницы есть кто-то, кроме её служебной учётки, — значит,
    это не песочница (ошибка в DEMO_COMPANY_CODE).

    Компания — явным условием: в count() без сущности в списке колонок
    ORM-фильтр тенанта не срабатывает, а роль без RLS (суперпользователь
    в CI) посчитала бы людей всех компаний."""
    count = await session.scalar(
        select(func.count(User.id)).where(
            User.tenant_id == tenant_id,
            User.account_id.is_distinct_from(demo_account_id),
        )
    )
    return bool(count)
