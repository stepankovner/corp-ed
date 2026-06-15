class DomainError(Exception):
    """Базовый класс для всех доменных исключений."""


class TenantContextMissingError(Exception):
    """Запрос к тенант-скоупным данным без установленного тенанта в контексте.

    Признак бага: не выставлен TenantMiddleware или запрос идёт в обход.
    Серверная ошибка (500), клиент исправить не может.
    """


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
