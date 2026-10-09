"""Фото профиля (ТЗ §4).

Загруженный файл не хранится как есть: Pillow открывает его (с потолком
на число пикселей — защита от «бомб» распаковки), поворачивает по EXIF,
вырезает квадрат по центру и сохраняет 256×256 WebP. Метаданные снимка —
геометка, модель телефона, дата — не переживают перекодирование.
Декодирует картинку не процесс API, а дочерний процесс песочницы с
лимитами и без сети (ingest/images.py, ingest/sandbox.py).

Тег <img> не умеет слать заголовок Authorization, а access-токен живёт
только в памяти вкладки. Поэтому фото отдаётся по подписанной ссылке:
её получают только те, кому показан профиль (сам человек и коллеги по
компании), подпись — HMAC от SECRET_KEY, срок — до конца следующих
суток. Ссылка одна на сутки: браузер берёт фото из кэша.
"""

import hashlib
import hmac
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from corp_ed.core.config import get_settings
from corp_ed.core.exceptions import DomainError, ServiceUnavailableError
from corp_ed.domain.models import Account, AccountAvatar
from corp_ed.ingest.images import ImageError, ImageKind
from corp_ed.ingest.sandbox import process_image_isolated

MAX_AVATAR_BYTES = 5 * 1024 * 1024
"""Исходный файл: фото с телефона редко больше 5 МБ. Проверяется до
запуска песочницы."""
TOO_MANY_PIXELS_MESSAGE = "Картинка слишком большая — до 40 мегапикселей"
URL_LIFETIME_S = 86_400
_DAY = 86_400


class InvalidAvatarError(DomainError):
    """Файл — не фото в JPEG, PNG или WebP либо слишком большой. HTTP 400."""

    code = "invalid_avatar"

    def __init__(
        self, message: str = "Загрузите фото в JPEG, PNG или WebP до 5 МБ"
    ) -> None:
        super().__init__(message)


def _signing_key() -> bytes:
    # Отдельный ключ от SECRET_KEY: подпись ссылки на фото не подходит ни
    # к токенам, ни к чему-то ещё.
    secret = get_settings().secret_key.get_secret_value().encode()
    return hmac.new(secret, b"kronto:avatar-url", hashlib.sha256).digest()


def _signature(account_id: UUID, version: str, expires: int) -> str:
    message = f"{account_id}:{version}:{expires}".encode()
    return hmac.new(_signing_key(), message, hashlib.sha256).hexdigest()[:32]


def avatar_url(account_id: UUID, version: str, now: float | None = None) -> str:
    """Подписанная ссылка на фото. Срок — конец следующих суток (UTC): весь
    день ссылка одна и та же, и браузер не качает фото заново."""
    moment = time.time() if now is None else now
    expires = (int(moment) // _DAY + 2) * _DAY
    sig = _signature(account_id, version, expires)
    return f"/api/v1/avatars/{account_id}?v={version}&exp={expires}&sig={sig}"


def check_signature(
    account_id: UUID, version: str, expires: int, sig: str, now: float | None = None
) -> bool:
    if expires < (time.time() if now is None else now):
        return False
    return hmac.compare_digest(_signature(account_id, version, expires), sig)


async def reencode_image(
    kind: ImageKind, raw: bytes, invalid: Callable[[str | None], DomainError]
) -> bytes:
    """Картинка → WebP 256×256 в песочнице. Любая неудача с файлом —
    invalid(текст): битый файл, не тот формат, сбой дочернего процесса;
    больше 40 Мп — со своим текстом. Песочница без фильтра сети в
    production — 503: дело не в файле."""
    try:
        return await process_image_isolated(kind, raw)
    except ImageError as exc:
        if exc.code == "sandbox_unavailable":
            raise ServiceUnavailableError() from None
        too_many = exc.code == "too_many_pixels"
        raise invalid(TOO_MANY_PIXELS_MESSAGE if too_many else None) from None


def _invalid_avatar(message: str | None) -> DomainError:
    return InvalidAvatarError(message) if message else InvalidAvatarError()


@dataclass(frozen=True)
class StoredAvatar:
    content: bytes
    version: str


class AvatarService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def save(self, account: Account, raw: bytes) -> str:
        """Новое фото; возвращает подписанную ссылку на него."""
        if not raw or len(raw) > MAX_AVATAR_BYTES:
            raise InvalidAvatarError()
        content = await reencode_image(ImageKind.AVATAR, raw, _invalid_avatar)
        version = hashlib.sha256(content).hexdigest()[:16]
        avatar = await self.session.get(AccountAvatar, account.id)
        if avatar is None:
            self.session.add(
                AccountAvatar(account_id=account.id, content=content, version=version)
            )
        else:
            avatar.content = content
            avatar.version = version
        await self.session.commit()
        return avatar_url(account.id, version)

    async def remove(self, account: Account) -> None:
        await self.session.execute(
            delete(AccountAvatar).where(AccountAvatar.account_id == account.id)
        )
        await self.session.commit()

    async def load(self, account_id: UUID, version: str) -> StoredAvatar | None:
        avatar = await self.session.get(AccountAvatar, account_id)
        if avatar is None or avatar.version != version:
            return None
        return StoredAvatar(content=avatar.content, version=avatar.version)

    async def versions(self, account_ids: list[UUID]) -> dict[UUID, str]:
        """Версии фото для списка людей — одним запросом, без содержимого."""
        if not account_ids:
            return {}
        result = await self.session.execute(
            select(AccountAvatar.account_id, AccountAvatar.version).where(
                AccountAvatar.account_id.in_(account_ids)
            )
        )
        return {row.account_id: row.version for row in result}

    async def url_for(self, account_id: UUID) -> str | None:
        version = (await self.versions([account_id])).get(account_id)
        return avatar_url(account_id, version) if version else None
