class DomainError(Exception):
    """Базовый класс для всех доменных исключений."""


class TenantContextMissingError(Exception):
    """Запрос к тенант-скоупным данным без установленного тенанта в контексте.

    Признак бага: не выставлен TenantMiddleware или запрос идёт в обход.
    Серверная ошибка (500), клиент исправить не может.
    """


class InvalidCredentialsError(DomainError):
    """Неверные учётные данные при логине (компания/email/пароль)."""

    def __init__(self) -> None:
        super().__init__("Неверный логин или пароль")


class NotAuthenticatedError(DomainError):
    """Запрос к защищённому ресурсу без валидного токена.

    Токена нет, он битый, истёк, или юзер из токена больше не активен.
    HTTP 401 — клиент не доказал, КТО он. Отличать от PermissionError (403),
    где клиент известен, но ему не хватает прав.
    """

    def __init__(self, detail: str = "Не аутентифицирован") -> None:
        super().__init__(detail)


class NotFoundError(DomainError):
    """Сущность не найдена."""


class ConflictError(DomainError):
    """Конфликт состояния (например, дубликат)."""


class CodedConflictError(ConflictError):
    """Конфликт с машинным кодом для фронта (answer_in_progress…). HTTP 409."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class PermissionError(DomainError):
    """Недостаточно прав."""


class EmailAlreadyExistsError(ConflictError):
    """Email уже зарегистрирован."""

    def __init__(self, email: str) -> None:
        super().__init__(f"Сотрудник с почтой {email} уже есть в компании")


class TenantMismatchError(Exception):
    """Попытка записи с tenant_id, не совпадающим с контекстом.

    Признак бага: объект создан с чужим тенантом или tenant_id
    изменён у существующей записи. Серверная ошибка (500),
    клиент исправить не может.
    """


class WeakPasswordError(DomainError):
    """Пароль не проходит политику (core/password_policy.py). HTTP 422."""


class PasswordChangeRequiredError(PermissionError):
    """Пароль выдан администратором и ещё не сменён.

    До смены доступны только /auth/me, /auth/change-password и
    /auth/logout: временный пароль видел кто-то кроме владельца.
    """

    def __init__(self) -> None:
        super().__init__("Требуется сменить временный пароль")


class LastAdminError(ConflictError):
    """Операция оставила бы компанию без активного администратора."""

    code = "last_admin"

    def __init__(
        self, message: str = "В компании должен остаться хотя бы один администратор"
    ) -> None:
        super().__init__(message)


class SelfModificationError(ConflictError):
    """Администратор пытается снять роль или заблокировать сам себя."""

    def __init__(self) -> None:
        super().__init__("Нельзя изменить собственную роль или заблокировать себя")


class SelfPasswordResetError(ConflictError):
    """Администратор выдаёт временный пароль сам себе.

    Сброс закрывает все сессии: не скопировал пароль — вышел из системы,
    а единственный администратор компании так теряет доступ.
    """

    def __init__(self) -> None:
        super().__init__("Свой пароль меняйте в меню профиля: «Сменить пароль»")


class SeatsLimitError(ConflictError):
    """Активных учёток уже столько, сколько оплаченных мест (решение 28.09).
    HTTP 409; текст — для того, кто упёрся: админа или присоединяющегося."""


class ServiceUnavailableError(DomainError):
    """Зависимость, без которой операцию нельзя выполнить безопасно, лежит.

    Например, Redis со счётчиками попыток входа: без него пароль можно
    перебирать, поэтому вход временно закрыт. HTTP 503.
    """

    def __init__(self) -> None:
        super().__init__("Сервис временно недоступен, попробуйте позже")


class UnacceptableFileError(DomainError):
    """Загруженный файл не принят: формат, кодировка, скан без текста.

    code — машинный код для фронта, текст — для человека. Подробностей
    парсера наружу нет. HTTP 415 для неподдерживаемого формата, иначе 422.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DuplicateMaterialError(ConflictError):
    """Такой же файл уже загружен в эту компанию."""

    def __init__(self, material_id: object) -> None:
        super().__init__("Этот файл уже загружен")
        self.material_id = material_id


class CreditsExhaustedError(DomainError):
    """Пул кредитов компании на месяц исчерпан. HTTP 402.

    Жёсткая остановка — решение команды (досье 10.2): без оплаты сверх
    лимита и без мягкой деградации.
    """

    def __init__(self) -> None:
        super().__init__(
            "Лимит обращений компании на этот месяц исчерпан. "
            "Обратитесь к администратору вашей компании"
        )


class ConnectorLimitError(ConflictError):
    """Технический потолок подключений на компанию (CONNECTOR_MAX_PER_TENANT
    или tenants.connector_limit) — защита от скрипта в любом тарифе.

    HTTP 409 с кодом connector_limit.
    """

    def __init__(self, limit: int) -> None:
        super().__init__(f"В компании не больше {limit} подключений")
        self.code = "connector_limit"


class TariffConnectorLimitError(ConflictError):
    """Тариф компании не даёт больше подключений (domain/tariffs.py).

    HTTP 409 с кодом tariff_connector_limit: админ видит, какой тариф
    снимает ограничение.
    """

    def __init__(self, tariff_title: str, limit: int) -> None:
        super().__init__(
            f"В тарифе «{tariff_title}» — до {limit} подключений. "
            "Больше — в тарифе «Расширенный»"
        )
        self.code = "tariff_connector_limit"


class ConnectorNotInTariffError(ConflictError):
    """Система вне базового списка — только в тарифе «Корпоративный».

    HTTP 409 с кодом connector_not_in_tariff.
    """

    def __init__(self, tariff_title: str) -> None:
        super().__init__(
            f"Эта система не входит в тариф «{tariff_title}» — "
            "она доступна в тарифе «Корпоративный»"
        )
        self.code = "connector_not_in_tariff"


class InvalidConnectorConfigError(DomainError):
    """Настройки коннектора не приняты: неизвестный вид, лишнее или
    пропущенное поле, адрес не прошёл проверку. code — для фронта. HTTP 422."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ConnectorStateError(ConflictError):
    """Действие не подходит к состоянию коннектора (синхронизировать
    приостановленный, задать учётные данные не в том режиме). HTTP 409."""


class InvalidInviteError(NotFoundError):
    """Приглашение не найдено, отозвано, истекло или исчерпано.

    Одно сообщение на все причины: держателю ссылки незачем знать, какая.
    """

    code = "invalid_invite"

    def __init__(self) -> None:
        super().__init__(
            "Приглашение недействительно или истекло. Попросите новую ссылку "
            "или код у администратора компании."
        )


class InviteEmailDomainError(DomainError):
    """Почта не из домена, которым админ ограничил ссылку. HTTP 422."""

    def __init__(self, domain: str, *, company: bool = False) -> None:
        super().__init__(
            f"В эту компанию можно вступить только с почтой @{domain}"
            if company
            else f"По этой ссылке можно присоединиться только с почтой @{domain}"
        )


class EmailNotVerifiedError(PermissionError):
    """Пароль верный, но почта не подтверждена (ТЗ §3). HTTP 403."""

    code = "email_not_verified"

    def __init__(self) -> None:
        super().__init__("Почта не подтверждена — введите код из письма")


class NoCompanyError(PermissionError):
    """Учётка без выбранной компании обращается к данным компании (ТЗ §2).

    Фронт по коду показывает экран «Вы ещё не в компании». HTTP 403.
    """

    code = "no_company"

    def __init__(self) -> None:
        super().__init__("Вы ещё не состоите в компании")


class MfaSetupRequiredError(PermissionError):
    """Администратору (или всем в компании с правилом strong) нужен
    надёжный второй фактор — приложение или ключ доступа (ТЗ §3). HTTP 403."""

    code = "mfa_setup_required"

    def __init__(self) -> None:
        super().__init__(
            "Включите вход через приложение-аутентификатор или ключ доступа — "
            "это обязательно для администраторов"
        )


class MembershipBlockedError(PermissionError):
    """Администратор заблокировал человека в этой компании. HTTP 403."""

    code = "membership_blocked"

    def __init__(self) -> None:
        super().__init__(
            "Администратор компании закрыл вам доступ. Если это ошибка — напишите ему."
        )


class InvalidEmailCodeError(DomainError):
    """Код или ссылка из письма неверны, истекли или использованы.

    Одно сообщение на все причины. HTTP 400.
    """

    code = "invalid_code"

    def __init__(self) -> None:
        super().__init__("Код или ссылка недействительны — запросите новое письмо")


class EmailTakenError(ConflictError):
    """Новая почта уже занята другой учёткой (смена почты). HTTP 409."""

    code = "email_taken"

    def __init__(self) -> None:
        super().__init__("Эта почта уже используется другой учётной записью")


class CompanyRequestExistsError(ConflictError):
    """У учётки уже есть заявка на рассмотрении. HTTP 409."""

    code = "company_request_exists"

    def __init__(self) -> None:
        super().__init__("Ваша заявка уже на рассмотрении — мы скоро ответим")


class InvalidLeadError(DomainError):
    """Заявка на созвон не прошла проверку (дата, окно, согласие). HTTP 422."""
