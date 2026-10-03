import enum
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
    text,
    true,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, deferred, mapped_column, relationship

from corp_ed.core.config import EMBEDDING_DIM
from corp_ed.core.database import Base
from corp_ed.domain.mixins import TenantMixin
from corp_ed.domain.tariffs import DEFAULT_TARIFF
from corp_ed.domain.types import DEFAULT_NOT_FOUND_MODE


class UserRole(enum.Enum):
    """Роль человека в компании — свойство членства, а не учётки (ТЗ §2):
    в одной компании он администратор, в другой — сотрудник.

    ADMIN — управляет документами и людьми компании, видит отладку
    поиска и отчёт о пробелах. EMPLOYEE — задаёт вопросы. Отдельных
    «владельца» и «редактора» нет (решение владельца продукта 03.10).
    Заводить компании не может ни одна роль: заявку одобряет команда
    Kronto (corp_ed.cli, позже — наша панель).
    """

    ADMIN = "admin"
    EMPLOYEE = "employee"


class MemberStatus(enum.Enum):
    """Состояние членства в компании.

    ACTIVE — работает и занимает место; BLOCKED — заблокирован админом,
    место не занимает; PENDING — вступил по приглашению с одобрением и
    ждёт админа; LEFT — ушёл сам или убран админом: учётка жива, доступа
    к компании нет, вернуться можно по новому приглашению.
    """

    ACTIVE = "active"
    BLOCKED = "blocked"
    PENDING = "pending"
    LEFT = "left"


class MaterialStatus(enum.Enum):
    """Где материал на пути к поиску.

    PENDING — поставлен в очередь, PROCESSING — воркер нарезает и считает
    эмбеддинги, READY — чанки на месте, FAILED — не получилось (причина —
    кодом в status_error, подробности только в логе).
    """

    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class IngestJobStatus(enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Tenant(Base):
    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint("seats > 0", name="ck_tenants_seats_positive"),
        CheckConstraint(
            "not_found_mode IN ('general', 'strict')",
            name="ck_tenants_not_found_mode",
        ),
        CheckConstraint(
            "tariff IN ('base', 'extended', 'enterprise')",
            name="ck_tenants_tariff",
        ),
        CheckConstraint(
            "connector_limit IS NULL OR connector_limit > 0",
            name="ck_tenants_connector_limit_positive",
        ),
        CheckConstraint(
            "mfa_policy IN ('any', 'strong')", name="ck_tenants_mfa_policy"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    company_code: Mapped[str] = mapped_column(unique=True, index=True)
    name: Mapped[str]
    # Приостановленная компания (неоплата, окончание пилота, инцидент):
    # вход закрыт, выданные токены перестают приниматься.
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
    # Оплаченные места. Пул кредитов на месяц = места × кредитов на место
    # (досье 10.2). Задаёт команда при подключении (CLI).
    seats: Mapped[int] = mapped_column(default=30, server_default="30")
    # Ответ, когда в документах ничего нет: strict или general
    # (NotFoundMode). По умолчанию general (DEFAULT_NOT_FOUND_MODE);
    # меняет команда через CLI.
    not_found_mode: Mapped[str] = mapped_column(
        String(16),
        default=DEFAULT_NOT_FOUND_MODE.value,
        server_default=DEFAULT_NOT_FOUND_MODE.value,
    )
    # Тариф (domain/tariffs.py, решение 30.09): сколько подключений и
    # какие системы. Задаёт команда через CLI.
    tariff: Mapped[str] = mapped_column(
        String(16), default=DEFAULT_TARIFF.value, server_default=DEFAULT_TARIFF.value
    )
    # Технический потолок подключений для этой компании; NULL — общий
    # CONNECTOR_MAX_PER_TENANT. Поднимает команда (cli set-tariff).
    connector_limit: Mapped[int | None]
    # Второй фактор (ТЗ §3): any — достаточно кода на почту; strong —
    # всем сотрудникам приложение или ключ доступа. Администраторам
    # strong обязателен всегда. Меняет администратор компании.
    mfa_policy: Mapped[str] = mapped_column(
        String(16), default="any", server_default="any"
    )
    # Галочка «Запомнить это устройство» на входе (ТЗ §3).
    allow_remember_device: Mapped[bool] = mapped_column(
        default=True, server_default=true()
    )
    # Домены почты компании (ТЗ §7): если заданы, вступить по любому
    # приглашению можно только с почтой этих доменов (и поддоменов).
    email_domains: Mapped[list[str]] = mapped_column(
        ARRAY(String(253)), default=list, server_default="{}"
    )


class Account(Base):
    """Учётная запись человека в kronto (ТЗ §2, решение 03.10).

    Не зависит от компании: почта, пароль, имя и подтверждение почты
    живут здесь, а роль и доступ — в членстве (User). Ушёл из компании —
    учётка остаётся и ждёт следующего приглашения.

    Не тенантская и не под RLS: вход ищет учётку по почте до того, как
    известна компания. Данных компаний здесь нет.
    """

    __tablename__ = "accounts"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # В casefold: Anna@Acme.ru и anna@acme.ru — одна учётка.
    email: Mapped[str] = mapped_column(String(254), unique=True)
    hashed_password: Mapped[str]
    # У перенесённых со старой схемы учёток имени может не быть: фронт
    # попросит его при входе. Новые без имени не регистрируются.
    first_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))
    # Профиль (ТЗ §4): видят коллеги по компаниям человека, вне их — никто.
    patronymic: Mapped[str | None] = mapped_column(String(100))
    # +79991234567: только цифры после «+», без пробелов (core/profile.py).
    phone: Mapped[str | None] = mapped_column(String(16))
    # Имя пользователя Telegram без «@».
    telegram: Mapped[str | None] = mapped_column(String(32))
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Согласие на обработку персональных данных при регистрации (152-ФЗ):
    # когда и с какой редакцией политики. NULL — учётка до 03.10.
    consented_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_policy_version: Mapped[str | None] = mapped_column(String(64))
    # Версия сессий учётки: смена пароля и «выйти везде» увеличивают её,
    # и все выданные access-токены перестают приниматься.
    token_version: Mapped[int] = mapped_column(default=0, server_default="0")
    # Пароль выдала команда (cli reset-password): до смены — только смена.
    must_change_password: Mapped[bool] = mapped_column(
        default=False, server_default=false()
    )
    # Приложение-аутентификатор (TOTP, ТЗ §3): секрет зашифрован SecretBox;
    # totp_last_step — последний принятый 30-секундный шаг (код нельзя
    # предъявить дважды).
    totp_secret: Mapped[str | None] = mapped_column(Text)
    totp_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    totp_last_step: Mapped[int | None] = mapped_column(BigInteger)
    # Компания, в которой человек был последней: после входа — она.
    last_tenant_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL")
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    @property
    def full_name(self) -> str | None:
        name = " ".join(p for p in (self.first_name, self.last_name) if p)
        return name or None


class AccountAvatar(Base):
    """Фото профиля (ТЗ §4): квадрат 256×256 в WebP, перекодированный на
    сервере — без метаданных исходного снимка (геометка, модель телефона).

    Отдельно от accounts: учётку читают на каждом запросе, а фото — только
    по своей ссылке. Не под RLS, как и учётка; выдаётся по подписанной
    ссылке, которую получают только коллеги (services/avatar_service.py).
    """

    __tablename__ = "account_avatars"

    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    content: Mapped[bytes] = mapped_column(LargeBinary)
    # Меняется с каждым новым фото: ссылка с прежней версией не отдаёт
    # новое фото из кэша браузера.
    version: Mapped[str] = mapped_column(String(16))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TenantLogo(Base):
    """Логотип компании (ТЗ §7): вписан в квадрат 256×256, WebP с
    прозрачностью — в переключателе компаний и в шапке.

    Как фото профиля — вне RLS: переключатель показывает логотипы всех
    компаний человека, а <img> не шлёт токен. Выдаётся по подписанной
    ссылке, которую получают только участники компании
    (services/company_service.py).
    """

    __tablename__ = "tenant_logos"

    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    content: Mapped[bytes] = mapped_column(LargeBinary)
    version: Mapped[str] = mapped_column(String(16))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Department(TenantMixin, Base):
    """Отдел компании (ТЗ §7): заводит администратор, человек выбирает свой
    в профиле. Дальше к отделам привязывается доступ к папкам (§5)."""

    __tablename__ = "departments"
    __table_args__ = (
        # «Продажи» и «продажи» — один отдел.
        Index(
            "uq_departments_tenant_name",
            "tenant_id",
            text("lower(name)"),
            unique=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Folder(TenantMixin, Base):
    """Папка загруженных документов (ТЗ §5, §7) с доступом по отделам.

    restricted=False — документы видят все сотрудники; True — только
    отделы из folder_departments и администраторы компании. Документы
    из источников (коннекторы) в папки не кладутся: у них права источника.
    Непустую папку удалить нельзя (RESTRICT): иначе документы закрытой
    папки стали бы видны всем.
    """

    __tablename__ = "folders"
    __table_args__ = (
        Index("uq_folders_tenant_name", "tenant_id", text("lower(name)"), unique=True),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(100))
    restricted: Mapped[bool] = mapped_column(default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FolderDepartment(TenantMixin, Base):
    """Отдел, которому открыта закрытая папка. Удалили отдел — пропал и
    доступ (CASCADE)."""

    __tablename__ = "folder_departments"

    folder_id: Mapped[UUID] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"), primary_key=True
    )
    department_id: Mapped[UUID] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class User(TenantMixin, Base):
    """Членство учётки в компании (ТЗ §2).

    Таблица и класс называются по-старому: на users.id ссылаются журнал
    вопросов, доступы к документам, подключения сотрудников — для них
    «пользователь» и был человеком внутри одной компании. Личное (почта,
    пароль, имя) — в Account.
    """

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "account_id", name="uq_user_tenant_account"),
        CheckConstraint(
            "status IN ('active', 'blocked', 'pending', 'left')",
            name="ck_users_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # NULL — учётку удалили: членство остаётся ради ссылок журнала
    # вопросов и показывается как «удалённый пользователь».
    account_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), index=True
    )
    role: Mapped[UserRole]
    status: Mapped[MemberStatus] = mapped_column(
        SAEnum(
            MemberStatus,
            native_enum=False,
            length=16,
            values_callable=lambda members: [m.value for m in members],
        ),
        default=MemberStatus.ACTIVE,
        server_default=MemberStatus.ACTIVE.value,
    )
    # Версия членства: смена роли, блокировка и удаление из компании
    # увеличивают её — access-токены этой компании отвергаются сразу.
    token_version: Mapped[int] = mapped_column(default=0, server_default="0")
    # Должность и отдел — свои в каждой компании (ТЗ §4): заполняет сам
    # человек, администратор может поправить.
    position: Mapped[str | None] = mapped_column(String(100))
    department_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("departments.id", ondelete="SET NULL"), index=True
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # joined: имя и почта нужны почти везде, где есть членство; accounts
    # не тенантская, фильтр по тенанту на соединение не влияет.
    account: Mapped[Account | None] = relationship(lazy="joined", innerjoin=False)

    @property
    def is_active(self) -> bool:
        return self.status is MemberStatus.ACTIVE

    @property
    def email(self) -> str | None:
        return self.account.email if self.account else None

    @property
    def full_name(self) -> str | None:
        return self.account.full_name if self.account else None


class Invite(TenantMixin, Base):
    """Приглашение в компанию: ссылка и код (решения 28.09 и 03.10).

    Админ отправляет ссылку или код куда угодно (рабочий чат, почта); по
    ним человек со своей учёткой kronto вступает в компанию. Хранятся
    только sha256 токена ссылки и кода, как у refresh-токенов: утечка
    таблицы не даёт действующих приглашений. Под RLS; компания по хешу
    находится через InviteLookup.
    """

    __tablename__ = "invites"
    __table_args__ = (
        CheckConstraint("max_uses > 0", name="ck_invites_max_uses_positive"),
        CheckConstraint("uses >= 0 AND uses <= max_uses", name="ck_invites_uses"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # sha256 кода приглашения (K7QM-4XPA) — той же ссылки в короткой
    # форме, чтобы продиктовать (ТЗ §2). NULL у ссылок до 03.10.
    code_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    max_uses: Mapped[int]
    uses: Mapped[int] = mapped_column(default=0, server_default="0")
    # Почта присоединяющегося — только в этом домене (и его поддоменах);
    # пусто — любая. Хранится в нижнем регистре, без «@».
    email_domain: Mapped[str | None] = mapped_column(String(253))
    # Вступивший ждёт одобрения администратора (по умолчанию — нет).
    requires_approval: Mapped[bool] = mapped_column(
        default=False, server_default=false()
    )
    # Роль вступающего. ADMIN — только у приглашения первого
    # администратора, которое выдаёт команда Kronto (cli); админ компании
    # создаёт приглашения сотрудников.
    role: Mapped[UserRole] = mapped_column(
        default=UserRole.EMPLOYEE, server_default=UserRole.EMPLOYEE.name
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class InviteLookup(Base):
    """Хеш ссылки или кода приглашения → компания.

    Приглашения под RLS, а человек со ссылкой или кодом компанию ещё не
    знает (в ссылке с 03.10 её нет, код — 8 символов). Эта таблица —
    только хеши и идентификаторы, без данных компании; по найденному
    tenant_id приглашение читается уже в его контексте.
    """

    __tablename__ = "invite_lookups"

    hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    invite_id: Mapped[UUID] = mapped_column(
        ForeignKey("invites.id", ondelete="CASCADE"), index=True
    )


class CompanyRequest(Base):
    """Заявка «Подключить компанию» от учётки без компании (ТЗ §2).

    Одобряет команда Kronto: создаётся компания, заявитель становится
    её администратором. Не тенантская: компании ещё нет.
    """

    __tablename__ = "company_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('new', 'approved', 'rejected', 'cancelled')",
            name="ck_company_requests_status",
        ),
        CheckConstraint(
            "seats IS NULL OR seats > 0", name="ck_company_requests_seats_positive"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    company_name: Mapped[str] = mapped_column(String(200))
    seats: Mapped[int | None]
    comment: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="new", server_default="new")
    tenant_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmailTokenPurpose(enum.Enum):
    VERIFY_EMAIL = "verify_email"
    RESET_PASSWORD = "reset_password"  # noqa: S105 — назначение, не пароль
    CHANGE_EMAIL = "change_email"
    # Код на прежний адрес — второй фактор смены почты без приложения.
    CHANGE_EMAIL_CODE = "change_email_code"
    REVERT_EMAIL = "revert_email"


class EmailToken(Base):
    """Одноразовая ссылка или код из письма (ТЗ §3).

    Хранятся только sha256 ссылки и кода. Код из 6 цифр — для
    подтверждения почты с телефона; его перебор ограничен попытками на
    сам токен и частотой запросов.
    """

    __tablename__ = "email_tokens"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('verify_email', 'reset_password', 'change_email', "
            "'change_email_code', 'revert_email')",
            name="ck_email_tokens_purpose",
        ),
        Index("ix_email_tokens_account_purpose", "account_id", "purpose"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE")
    )
    purpose: Mapped[str] = mapped_column(String(32))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    code_hash: Mapped[str | None] = mapped_column(String(64))
    # Смена почты: новый адрес (change_email) или прежний (revert_email).
    email: Mapped[str | None] = mapped_column(String(254))
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class OutboxEmail(Base):
    """Письмо в очереди на отправку (ТЗ §3).

    Запрос только кладёт письмо сюда в своей транзакции, отправляет
    воркер: сбой почты не ломает регистрацию, письмо уйдёт повторной
    попыткой. Текст после отправки стирается — в нём ссылки и коды.
    """

    __tablename__ = "outbox_emails"
    __table_args__ = (Index("ix_outbox_emails_pending", "sent_at", "next_attempt_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    to_email: Mapped[str] = mapped_column(String(254))
    kind: Mapped[str] = mapped_column(String(32))
    subject: Mapped[str] = mapped_column(String(200))
    text_body: Mapped[str] = mapped_column(Text)
    html_body: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class BackupCode(Base):
    """Резервный код второго фактора (ТЗ §3): 10 штук, каждый — один раз.

    На случай потерянного телефона. Хранится sha256 с солью учётки.
    """

    __tablename__ = "backup_codes"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    code_hash: Mapped[str] = mapped_column(String(64))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Passkey(Base):
    """Ключ доступа (WebAuthn, ТЗ §3): отпечаток, Face ID, Windows Hello,
    аппаратный ключ. Хранится открытый ключ — секрета у нас нет."""

    __tablename__ = "passkeys"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    credential_id: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    sign_count: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    transports: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), default=list, server_default="{}"
    )
    name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TrustedDevice(Base):
    """«Запомнить это устройство» (ТЗ §3): 30 дней без второго фактора.

    Браузер держит случайный токен в httpOnly-cookie, здесь — sha256.
    «Выйти везде» и смена пароля забывают все устройства.
    """

    __tablename__ = "trusted_devices"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_agent: Mapped[str | None] = mapped_column(String(300))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AuthChallenge(Base):
    """Незавершённый шаг входа или настройки второго фактора (ТЗ §3).

    login — пароль верный, ждём второй фактор (браузер держит токен,
    здесь — sha256); totp_setup — секрет приложения до подтверждения
    кодом; passkey_setup — challenge регистрации ключа. Короткий срок,
    одноразовый.
    """

    __tablename__ = "auth_challenges"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('login', 'totp_setup', 'passkey_setup')",
            name="ck_auth_challenges_purpose",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # Код на почту для входа — sha256 с солью учётки.
    email_code_hash: Mapped[str | None] = mapped_column(String(64))
    webauthn_challenge: Mapped[bytes | None] = mapped_column(LargeBinary)
    # Секрет TOTP до подтверждения — зашифрован SecretBox.
    payload: Mapped[str | None] = mapped_column(Text)
    remember: Mapped[bool] = mapped_column(default=False, server_default=false())
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Lead(Base):
    """Заявка на созвон со страницы тарифов (досье 10.1, решение 28.09).

    Не тенантская: клиента ещё нет. Персональные данные (имя, телефон,
    почта) — только то, что нужно, чтобы перезвонить; IP не хранится.
    Согласие записывается с версией политики. Срок хранения —
    LEADS_RETENTION_DAYS, удаляет `cli purge`.
    """

    __tablename__ = "leads"
    __table_args__ = (
        CheckConstraint(
            "status IN ('new', 'contacted', 'scheduled', 'rejected')",
            name="ck_leads_status",
        ),
        CheckConstraint(
            "tariff IN ('base', 'extended', 'enterprise')", name="ck_leads_tariff"
        ),
        CheckConstraint("seats > 0", name="ck_leads_seats_positive"),
        Index("ix_leads_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    company_name: Mapped[str] = mapped_column(String(200))
    contact_name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(254))
    seats: Mapped[int]
    tariff: Mapped[str] = mapped_column(String(16))
    preferred_date: Mapped[date]
    preferred_slot: Mapped[str] = mapped_column(String(16))
    comment: Mapped[str | None] = mapped_column(Text)
    policy_version: Mapped[str] = mapped_column(String(64))
    consented_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="new", server_default="new")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RefreshToken(Base):
    """Выданный refresh-токен (хранится только sha256).

    Принадлежит учётке, не компании: вход — один на человека. tenant_id
    и user_id — компания, выбранная в этой сессии, и членство в ней
    (NULL — человек без компании или ещё не выбрал). Переключение
    компании выдаёт новую пару с другим tenant_id.

    Не TenantMixin намеренно: токен предъявляют до того, как известен
    тенант, — поиск идёт по хешу.

    family_id объединяет цепочку ротаций одного входа. Повторное
    предъявление уже использованного токена — признак кражи: отзывается
    вся семья (RFC 9700, раздел 4.14.2).
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    tenant_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    family_id: Mapped[UUID] = mapped_column(index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # «Запомнить это устройство» не отмечено: cookie без срока, сессия
    # кончается с браузером (ТЗ §3).
    remember: Mapped[bool] = mapped_column(default=True, server_default=true())
    # Для списка сеансов в настройках (ТЗ §3): браузер и адрес при выдаче.
    user_agent: Mapped[str | None] = mapped_column(String(300))
    ip: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Material(TenantMixin, Base):
    __tablename__ = "materials"
    __table_args__ = (
        # Один и тот же файл дважды в одной компании — дубль выдержек в
        # выдаче и двойная цена эмбеддингов. Только для ручных загрузок:
        # один файл на двух дисках коннекторов — два документа с разными
        # ссылками, sha256 у них — признак «не изменился», не ключ.
        Index(
            "uq_materials_tenant_sha256",
            "tenant_id",
            "source_sha256",
            unique=True,
            postgresql_where=text("source_sha256 IS NOT NULL AND connector_id IS NULL"),
        ),
        Index(
            "uq_materials_connector_external_id",
            "connector_id",
            "external_id",
            unique=True,
            postgresql_where=text("connector_id IS NOT NULL"),
        ),
        CheckConstraint(
            "visibility IN ('tenant', 'restricted')", name="ck_materials_visibility"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    title: Mapped[str]
    content: Mapped[str]
    # Исходный файл не хранится (меньше персональных данных у нас) —
    # только извлечённый текст и сведения о файле для справки и дублей.
    # Для текста, вставленного в форму, поля пустые.
    source_filename: Mapped[str | None] = mapped_column(String(255))
    source_format: Mapped[str | None] = mapped_column(String(16))
    source_sha256: Mapped[str | None] = mapped_column(String(64))
    source_size: Mapped[int | None]
    status: Mapped[MaterialStatus] = mapped_column(
        default=MaterialStatus.PENDING, server_default="PENDING"
    )
    # Код причины, а не текст исключения: админ компании видит его в
    # интерфейсе, а трассировка и ответ провайдера — только в логе.
    status_error: Mapped[str | None] = mapped_column(String(64))
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Документ из источника (коннектор): стабильный id в системе,
    # ссылка для сотрудника, версия (etag / дата / ревизия) для
    # сравнения без скачивания. У ручных загрузок всё пусто.
    connector_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("connectors.id", ondelete="CASCADE"), index=True
    )
    external_id: Mapped[str | None] = mapped_column(String(512))
    source_url: Mapped[str | None] = mapped_column(String(2048))
    external_version: Mapped[str | None] = mapped_column(String(128))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # tenant — видят все сотрудники; restricted — только по material_access
    # (MaterialVisibility). Проверяется в поиске чанков.
    visibility: Mapped[str] = mapped_column(
        String(16), default="tenant", server_default="tenant"
    )
    # Папка загруженного документа (ТЗ §5): закрытая папка сужает круг
    # тех, кто видит документ, до своих отделов и администраторов.
    folder_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("folders.id", ondelete="RESTRICT"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MaterialAccess(TenantMixin, Base):
    """Кому виден документ с visibility = restricted.

    Заполняет синхронизация коннектора: в режиме organization — из ACL
    источника по почте сотрудника, в режиме per_user — фактом «документ
    есть в листинге этого сотрудника». Удаление документа или
    сотрудника убирает строки каскадом.
    """

    __tablename__ = "material_access"

    material_id: Mapped[UUID] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class Chunk(TenantMixin, Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("material_id", "position", name="uq_chunk_material_position"),
        Index("ix_chunks_fts", "fts", postgresql_using="gin"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    material_id: Mapped[UUID] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int]
    # Стек заголовков секции без названия документа: ["Раздел 3", "3.2 …"].
    heading_path: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default="{}"
    )
    # Крошки + текст без разметки. По нему считается эмбеддинг; хранится,
    # чтобы из него же строилась полнотекстовая ветка поиска (M1).
    embed_text: Mapped[str] = mapped_column(Text, default="", server_default="")
    # Полнотекстовая ветка гибридного поиска (M1, BH-12). Считает сама
    # база из embed_text — тот же текст, что у эмбеддинга, поэтому ветки
    # видят одно и то же. deferred: в Python вектор лексем не нужен.
    fts: Mapped[str] = deferred(
        mapped_column(
            TSVECTOR,
            Computed("to_tsvector('russian', embed_text)", persisted=True),
        )
    )
    # Крошки + Markdown (llm_text) — то, что уходит в промпт.
    content: Mapped[str]
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    model: Mapped[str]
    model_version: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AuditEvent(Base):
    """Запись журнала аудита: кто, что, над чем, когда, откуда.

    Не TenantMixin намеренно: часть событий происходит до того, как
    тенант известен (неудачный вход с неверным кодом компании), и такие
    записи должны сохраниться. Поэтому tenant_id допускает NULL, а
    выборки для админа фильтруются по нему явно.

    Журнал только дописывается: UPDATE запрещён триггером в базе,
    DELETE — только для записей старше срока хранения (см. миграцию).
    Защита на уровне базы, а не кода: запись о действии не должна
    исчезать вместе с тем, кто это действие совершил.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL")
    )
    actor_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    ip: Mapped[str | None] = mapped_column(String(45))
    request_id: Mapped[str | None] = mapped_column(String(36))
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class IngestJob(Base):
    """Задача фонового ингеста (нарезка + эмбеддинги + замена чанков).

    Очередь — таблица в Postgres: задача ставится в той же транзакции,
    что и материал, поэтому не теряется между базой и брокером и не
    появляется для материала, чья транзакция откатилась.

    Не TenantMixin и не под RLS намеренно: воркер забирает задачи всех
    компаний по очереди и до выбора задачи тенанта не знает. В таблице
    только идентификаторы; содержимое материала воркер читает уже в
    контексте тенанта задачи.
    """

    __tablename__ = "ingest_jobs"
    __table_args__ = (
        # Не больше одной активной задачи на материал: повторный запрос
        # переиндексации не плодит параллельные пересчёты одного документа.
        Index(
            "uq_ingest_jobs_active_material",
            "material_id",
            unique=True,
            postgresql_where=text("status IN ('QUEUED', 'RUNNING')"),
        ),
        Index("ix_ingest_jobs_queue", "status", "run_after"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    material_id: Mapped[UUID] = mapped_column(
        ForeignKey("materials.id", ondelete="CASCADE")
    )
    status: Mapped[IngestJobStatus] = mapped_column(default=IngestJobStatus.QUEUED)
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(64))
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class QaLog(TenantMixin, Base):
    """Журнал вопросов и ответов (BH-20).

    Нужен для трёх вещей: привязать 👍/👎 и eval к версии промпта и
    модели, собрать отчёт о пробелах (вопросы, на которые в документах
    ответа нет) и считать расход кредитов компании.

    Вопрос хранится ПОСЛЕ mask_pii (почта, телефоны, паспорта, ФИО):
    для подписи кластеров пробелов текст нужен, но персональные данные в
    нём — нет. Срок хранения — QA_LOG_RETENTION_DAYS, удаляет команда
    purge. Ответ модели не хранится — и для памяти диалога тоже: реплики
    живут в Redis несколько часов (core/dialogue_store.py).
    """

    __tablename__ = "qa_log"
    __table_args__ = (
        Index("ix_qa_log_tenant_created", "tenant_id", "created_at"),
        CheckConstraint("feedback IN (-1, 1)", name="ck_qa_log_feedback"),
        CheckConstraint(
            "origin IN ('documents', 'general_knowledge', 'none')",
            name="ck_qa_log_origin",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    question: Mapped[str] = mapped_column(Text)
    # Тот же вектор, что ушёл в поиск (не считать второй раз); по нему
    # кластеризуются пробелы. Размерность — как у chunks.
    question_embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32))
    # Пусто, если модель не вызывалась (строгий отказ без выдержек).
    llm_model: Mapped[str | None] = mapped_column(String(64))
    # Версия из ответа провайдера (BH-26): алиас /latest молча меняет
    # модель, а метрики по журналу должны быть привязаны к настоящей.
    llm_model_version: Mapped[str | None] = mapped_column(String(128))
    best_vector_distance: Mapped[float | None]
    best_fulltext_score: Mapped[float | None]
    answer_given: Mapped[bool]
    origin: Mapped[str] = mapped_column(String(32))
    source_chunk_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(Uuid), default=list, server_default="{}"
    )
    input_tokens: Mapped[int] = mapped_column(default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(default=0, server_default="0")
    credits: Mapped[int] = mapped_column(default=0, server_default="0")
    feedback: Mapped[int | None] = mapped_column(SmallInteger)
    # Что не так с ответом (ТЗ §6): причина из списка и комментарий —
    # после mask_pii, как вопрос. Видны только обезличенно.
    feedback_reason: Mapped[str | None] = mapped_column(String(32))
    feedback_comment: Mapped[str | None] = mapped_column(Text)
    # Сколько выдержек из вложения сотрудника ушло в промпт (ТЗ §6): такие
    # ответы — не по базе компании, eval и отчёт о пробелах их различают.
    attachment_chunks: Mapped[int] = mapped_column(default=0, server_default="0")
    # Заполняет ночная задача отчёта о пробелах (classify_miss).
    miss_kind: Mapped[str | None] = mapped_column(String(32))
    # Память диалога (BH-28). Сами реплики — в Redis, не здесь
    # (core/dialogue_store.py); журнал знает только, к какому диалогу
    # относится вопрос и как его поняли. standalone_question — после
    # mask_pii, как question; NULL — истории не было, искали по question.
    conversation_id: Mapped[UUID | None] = mapped_column(Uuid)
    standalone_question: Mapped[str | None] = mapped_column(Text)
    condense_prompt_version: Mapped[str | None] = mapped_column(String(32))
    history_turns: Mapped[int] = mapped_column(default=0, server_default="0")
    # Реранкер (M3, BH-32): модель, если порядок выдержек дал он; NULL —
    # порядок вектора (выключен, нечего переставлять или не ответил
    # вовремя). rerank_ms — сколько ждали реранкер, и при сбое тоже.
    rerank_model: Mapped[str | None] = mapped_column(String(128))
    rerank_ms: Mapped[int | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class GlossaryTerm(TenantMixin, Base):
    """Сокращение компании и его расшифровка (M5, BH-14).

    Заполняет админ компании. Перед поиском expand_query (ML) дописывает
    к вопросу расшифровки найденных терминов: «Как оформить ДМС?» находит
    документ, где написано «добровольное медицинское страхование».
    Расшифровки идут только в поиск — в промпт модели уходит исходный
    вопрос сотрудника.
    """

    __tablename__ = "glossary_terms"
    __table_args__ = (
        # «ДМС» и «дмс» — один термин: expand_query ищет без учёта регистра.
        Index(
            "uq_glossary_terms_tenant_term",
            "tenant_id",
            text("lower(term)"),
            unique=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    term: Mapped[str] = mapped_column(String(64))
    expansion: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class GapCluster(TenantMixin, Base):
    """Пробел в документах: группа похожих вопросов без ответа (BH-22).

    Пересобирается ночной задачей (services/gap_report_service.py) из
    qa_log за окно. id и статус переживают пересборку, если группа
    узнаётся по общим вопросам. title и missing — подпись модели
    (промпт gaps-v1) по вопросам после mask_pii.
    """

    __tablename__ = "gap_clusters"
    __table_args__ = (
        Index("ix_gap_clusters_tenant_priority", "tenant_id", "priority"),
        CheckConstraint(
            "status IN ('new', 'in_progress', 'resolved', 'dismissed')",
            name="ck_gap_clusters_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    title: Mapped[str] = mapped_column(String(200))
    missing: Mapped[str] = mapped_column(Text, default="", server_default="")
    priority: Mapped[float] = mapped_column(Float)
    question_count: Mapped[int]
    user_count: Mapped[int]
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="new", server_default="new")
    # Версия промпта подписи; пусто — подписать не удалось, повторить
    # следующей ночью. Смена версии — повод подписать заново.
    prompt_version: Mapped[str] = mapped_column(String(32), default="")
    # Векторы разных моделей эмбеддингов не кластеризуются вместе.
    embedding_model: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class GapClusterQuestion(TenantMixin, Base):
    """Вопрос из qa_log в кластере пробела.

    Удаление строки журнала (срок хранения) убирает её и отсюда.
    """

    __tablename__ = "gap_cluster_questions"

    cluster_id: Mapped[UUID] = mapped_column(
        ForeignKey("gap_clusters.id", ondelete="CASCADE"), primary_key=True
    )
    qa_log_id: Mapped[UUID] = mapped_column(
        ForeignKey("qa_log.id", ondelete="CASCADE"), primary_key=True, index=True
    )


class Connector(TenantMixin, Base):
    """Подключение компании к одной системе-источнику документов.

    kind — система (bitrix24, confluence, yandex360), modules — какие её
    части читать (диск, база знаний, пространства). Один портал — одно
    подключение (решение команды 25.09). config — НЕсекретные настройки
    (адрес портала, корневая папка); credentials — учётные данные режима
    organization, зашифрованные SecretBox, в API никогда не отдаются.
    В режиме per_user учётные данные у каждого сотрудника свои
    (ConnectorUserGrant), здесь пусто.
    """

    __tablename__ = "connectors"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('organization', 'per_user')", name="ck_connectors_mode"
        ),
        CheckConstraint(
            "status IN ('active', 'paused', 'error')", name="ck_connectors_status"
        ),
        CheckConstraint(
            "sync_interval_minutes BETWEEN 15 AND 1440",
            name="ck_connectors_sync_interval",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(100))
    mode: Mapped[str] = mapped_column(String(16))
    modules: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), default=list, server_default="{}"
    )
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}"
    )
    # deferred: шифротекст не должен подниматься в память API на каждый
    # список коннекторов — он нужен только воркеру и ручке проверки.
    credentials: Mapped[str | None] = deferred(mapped_column(Text))
    credentials_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(16), default="active", server_default="active"
    )
    sync_interval_minutes: Mapped[int] = mapped_column(default=60, server_default="60")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ConnectorUserGrant(TenantMixin, Base):
    """Авторизация сотрудника в коннекторе режима per_user.

    Хранит его токены (зашифрованы SecretBox) и состояние: источник
    отверг токен — expired, сотрудник отключился — revoked. Документы
    сотрудника видны ему через material_access, которую заполняет
    синхронизация его листингом.
    """

    __tablename__ = "connector_user_grants"
    __table_args__ = (
        UniqueConstraint("connector_id", "user_id", name="uq_grant_connector_user"),
        CheckConstraint(
            "status IN ('active', 'expired', 'revoked')", name="ck_grants_status"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    connector_id: Mapped[UUID] = mapped_column(
        ForeignKey("connectors.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    credentials: Mapped[str] = deferred(mapped_column(Text))
    # Идентификатор сотрудника в источнике, если адаптер его знает.
    external_user_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(
        String(16), default="active", server_default="active"
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ConnectorSyncRun(TenantMixin, Base):
    """Журнал запусков синхронизации: админу — что и когда произошло,
    команде — разбор сбоев. stats: seen / added / updated / removed /
    skipped / failed. Хранится CONNECTOR_SYNC_RUN_RETENTION_DAYS (purge)."""

    __tablename__ = "connector_sync_runs"
    __table_args__ = (
        Index("ix_sync_runs_connector_started", "connector_id", "started_at"),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'partial', 'failed')",
            name="ck_sync_runs_status",
        ),
        CheckConstraint(
            "trigger IN ('schedule', 'manual')", name="ck_sync_runs_trigger"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    connector_id: Mapped[UUID] = mapped_column(
        ForeignKey("connectors.id", ondelete="CASCADE")
    )
    trigger: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(
        String(16), default="running", server_default="running"
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}"
    )
    error_code: Mapped[str | None] = mapped_column(String(64))


class ConnectorSyncJob(Base):
    """Очередь синхронизации коннекторов — по образцу IngestJob.

    Не под RLS по той же причине: воркер выбирает задачу до того, как
    знает тенанта. Отдельная таблица, а не обобщение ingest_jobs: другой
    payload (коннектор, а не материал) и другая семантика повтора.
    """

    __tablename__ = "connector_sync_jobs"
    __table_args__ = (
        Index(
            "uq_sync_jobs_active_connector",
            "connector_id",
            unique=True,
            postgresql_where=text("status IN ('QUEUED', 'RUNNING')"),
        ),
        Index("ix_sync_jobs_queue", "status", "run_after"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    connector_id: Mapped[UUID] = mapped_column(
        ForeignKey("connectors.id", ondelete="CASCADE")
    )
    trigger: Mapped[str] = mapped_column(
        String(16), default="schedule", server_default="schedule"
    )
    status: Mapped[IngestJobStatus] = mapped_column(default=IngestJobStatus.QUEUED)
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(64))
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# --- Чат (ТЗ §6) ------------------------------------------------------------


class Conversation(TenantMixin, Base):
    """Диалог сотрудника с ассистентом (ТЗ §6): список слева, как в Claude.

    Видит только сам человек (user_id — его членство в компании) и те, с
    кем он поделился ссылкой внутри компании (share_token). Администратор
    чужих диалогов не видит: у него только обезличенная статистика.

    Сообщения — дерево (parent_id): правка вопроса и «Ответить заново»
    добавляют ветку, прежняя остаётся и переключается стрелками.
    current_message_id — лист показанной ветки; без внешнего ключа:
    сообщения ссылаются на диалог, и круговая зависимость таблиц мешала
    бы вставке и удалению.
    """

    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_owner_updated", "user_id", "updated_at"),
        UniqueConstraint("share_token", name="uq_conversations_share_token"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(120))
    pinned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_message_id: Mapped[UUID | None] = mapped_column(Uuid)
    # Ссылка «поделиться»: случайный токен; снимок — ветка до
    # shared_message_id на момент, когда поделились (как в ChatGPT).
    share_token: Mapped[str | None] = mapped_column(String(64))
    shared_message_id: Mapped[UUID | None] = mapped_column(Uuid)
    shared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ChatMessage(TenantMixin, Base):
    """Вопрос (role=user) или ответ (role=assistant) в диалоге.

    Текст ответа хранится — иначе диалог не открыть снова; это решение
    ТЗ от 03.10 вместо реплик только в Redis (BH-28). Журнал qa_log
    по-прежнему без ответа: в нём обезличенная статистика.

    sources — снимок выдержек ответа: при показе фрагмент документа
    открывается, только если документ жив и доступен смотрящему.
    """

    __tablename__ = "chat_messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant')", name="ck_chat_messages_role"),
        CheckConstraint(
            "status IN ('complete', 'generating', 'stopped', 'failed')",
            name="ck_chat_messages_status",
        ),
        CheckConstraint(
            "origin IS NULL OR origin IN ('documents', 'general_knowledge', 'none')",
            name="ck_chat_messages_origin",
        ),
        CheckConstraint("feedback IN (-1, 1)", name="ck_chat_messages_feedback"),
        Index("ix_chat_messages_conversation", "conversation_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE")
    )
    parent_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default="", server_default="")
    status: Mapped[str] = mapped_column(
        String(16), default="complete", server_default="complete"
    )
    origin: Mapped[str | None] = mapped_column(String(32))
    sources: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    # Вложения вопроса (chat_attachments.id); правка вопроса их наследует.
    attachment_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(Uuid), default=list, server_default="{}"
    )
    qa_log_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("qa_log.id", ondelete="SET NULL")
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    feedback: Mapped[int | None] = mapped_column(SmallInteger)
    feedback_reason: Mapped[str | None] = mapped_column(String(32))
    feedback_comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ChatAttachment(TenantMixin, Base):
    """Файл к вопросу («спроси по этому договору», ТЗ §6).

    В базу компании не попадает: ни в поиск коллег, ни в документы.
    Хранится только извлечённый текст по фрагментам — исходный файл нет.
    conversation_id пуст, пока вопрос с файлом не отправлен; такие
    вложения удаляет purge через сутки. Удаление диалога удаляет и их.
    """

    __tablename__ = "chat_attachments"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(255))
    source_format: Mapped[str] = mapped_column(String(16))
    size: Mapped[int]
    tokens: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ChatAttachmentChunk(TenantMixin, Base):
    """Фрагмент вложения. Эмбеддинг — только у файлов больше бюджета
    выдержек: маленький файл целиком уходит в промпт."""

    __tablename__ = "chat_attachment_chunks"
    __table_args__ = (
        UniqueConstraint(
            "attachment_id", "position", name="uq_chat_attachment_chunk_position"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    attachment_id: Mapped[UUID] = mapped_column(
        ForeignKey("chat_attachments.id", ondelete="CASCADE")
    )
    position: Mapped[int]
    heading_path: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default="{}"
    )
    embed_text: Mapped[str] = mapped_column(Text, default="", server_default="")
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))


class ChatSuggestion(TenantMixin, Base):
    """Подсказка вопроса на пустом экране чата, заданная админом (ТЗ §6)."""

    __tablename__ = "chat_suggestions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    text: Mapped[str] = mapped_column(String(200))
    position: Mapped[int] = mapped_column(default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
