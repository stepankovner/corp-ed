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
