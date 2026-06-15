import structlog

from lms.api.v1.schemas.user import UserCreate
from lms.core.exceptions import EmailAlreadyExistsError
from lms.core.security import hash_password
from lms.domain.models import User
from lms.repositories.user_repository import UserRepository

logger = structlog.get_logger()


class UserService:
    """Бизнес-логика работы с пользователями."""

    def __init__(self, repository: UserRepository) -> None:
        self.repository = repository

    async def register(self, data: UserCreate) -> User:
        existing = await self.repository.get_by_email(data.email)
        if existing is not None:
            raise EmailAlreadyExistsError(data.email)

        user = User(
            email=data.email,
            hashed_password=hash_password(data.password),
            full_name=data.full_name,
        )
        created_user = await self.repository.create(user)
        logger.info(
            "user_registered",
            user_id=created_user.id,
            email=created_user.email,
        )
        return created_user
