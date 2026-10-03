"""Поля профиля (ТЗ §4): телефон и Telegram в одном виде, как бы их ни ввели.

Телефон — «+» и цифры (E.164): так по нему звонит ссылка tel: с любого
устройства. Восьмёрка в начале российского номера становится «+7».
Telegram — имя пользователя без «@»: из него собирается ссылка t.me.
"""

import re

from corp_ed.core.exceptions import DomainError

_PHONE_NOISE = re.compile(r"[\s\-().]")
_TELEGRAM_LINK = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me)/", re.IGNORECASE)
# Правила Telegram: 5–32 символа, латиница, цифры и «_», начинается с буквы.
_TELEGRAM_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


class InvalidProfileFieldError(DomainError):
    """Поле профиля не похоже на телефон или имя в Telegram. HTTP 400."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def normalize_phone(value: str | None) -> str | None:
    """+7 (999) 123-45-67, 8 999 123 45 67 → +79991234567; пусто — None."""
    if value is None or not value.strip():
        return None
    raw = _PHONE_NOISE.sub("", value.strip())
    digits = raw[1:] if raw.startswith("+") else raw
    if not digits.isdigit():
        raise InvalidProfileFieldError(
            "invalid_phone", "Телефон — только цифры, можно с «+» в начале"
        )
    if not raw.startswith("+") and len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    # E.164: не длиннее 15 цифр; короче 10 — это не полный номер.
    if not 10 <= len(digits) <= 15:
        raise InvalidProfileFieldError(
            "invalid_phone", "Укажите номер полностью, с кодом страны: +7 999 123-45-67"
        )
    return "+" + digits


def normalize_telegram(value: str | None) -> str | None:
    """@anna_s, anna_s, https://t.me/anna_s → anna_s; пусто — None."""
    if value is None or not value.strip():
        return None
    name = _TELEGRAM_LINK.sub("", value.strip()).lstrip("@").rstrip("/")
    if not _TELEGRAM_NAME.fullmatch(name):
        raise InvalidProfileFieldError(
            "invalid_telegram",
            "Имя в Telegram — от 5 до 32 латинских букв, цифр и «_», "
            "начинается с буквы",
        )
    return name
