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


class PermissionError(DomainError):
    """Недостаточно прав."""


class EmailAlreadyExistsError(ConflictError):
    """Email уже зарегистрирован."""

    def __init__(self, email: str) -> None:
        super().__init__(f"User with email '{email}' already exists")


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

    def __init__(self) -> None:
        super().__init__("В компании должен остаться хотя бы один администратор")


class SelfModificationError(ConflictError):
    """Администратор пытается снять роль или заблокировать сам себя."""

    def __init__(self) -> None:
        super().__init__("Нельзя изменить собственную роль или заблокировать себя")


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
    """Технический потолок подключений на компанию (CONNECTOR_MAX_PER_TENANT).

    Не тарифная граница: тариф строится от мест (решение команды 25.09).
    HTTP 409 с кодом connector_limit.
    """

    def __init__(self, limit: int) -> None:
        super().__init__(f"В компании не больше {limit} подключений")
        self.code = "connector_limit"


class InvalidConnectorConfigError(DomainError):
    """Настройки коннектора не приняты: неизвестный вид, лишнее или
    пропущенное поле, адрес не прошёл проверку. code — для фронта. HTTP 422."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ConnectorStateError(ConflictError):
    """Действие не подходит к состоянию коннектора (синхронизировать
    приостановленный, задать учётные данные не в том режиме). HTTP 409."""
