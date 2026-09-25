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
