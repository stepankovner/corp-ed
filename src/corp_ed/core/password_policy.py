"""Требования к паролю.

Ориентир — OWASP ASVS 5.0, раздел 6.2 и NIST SP 800-63B: длина важнее
«сложности». Правила «цифра + заглавная + спецсимвол» не требуются:
они учат пользователей писать Password1! и не добавляют стойкости.

- Минимум 12 символов (ASVS 6.2.1).
- Максимум 128: argon2 считает хеш от всей строки, а мегабайтный
  «пароль» — дешёвый способ занять CPU сервера (ASVS 6.2.9 требует
  разрешать минимум 64 символа, 128 — с запасом).
- Не из списка самых распространённых паролей (ASVS 6.2.4). Список
  короткий и встроенный: сетевые проверки (Have I Been Pwned) уводят
  данные за периметр, а полный словарь — лишняя зависимость. Словарь
  расширяется здесь же.
- Не содержит почту пользователя или её локальную часть: такой пароль
  подбирается первым.
"""

from corp_ed.core.exceptions import WeakPasswordError

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128

_COMMON_PASSWORDS = frozenset(
    {
        "123456789012",
        "1234567890123",
        "12345678901234",
        "qwertyuiop12",
        "qwertyuiop123",
        "qwerty123456",
        "qwerty1234567",
        "1q2w3e4r5t6y",
        "1q2w3e4r5t6y7u",
        "q1w2e3r4t5y6",
        "zaq12wsxcde3",
        "1qaz2wsx3edc",
        "asdfghjkl123",
        "password1234",
        "password12345",
        "passwordpassword",
        "iloveyou1234",
        "adminadmin123",
        "administrator",
        "administrator1",
        "welcome12345",
        "letmein12345",
        "changeme1234",
        "default12345",
        "111111111111",
        "000000000000",
        "123123123123",
        "abcdefghijkl",
        "abc123abc123",
        "йцукенгшщзхъ",
        "йцукен123456",
        "пароль123456",
        "парольпароль",
        "qwertyqwerty",
        "passw0rd1234",
        "p@ssw0rd1234",
        "p@ssword1234",
        "summer2026!!",
        "winter2026!!",
        "company12345",
    }
)


def validate_password(password: str, *, email: str | None = None) -> None:
    """Проверить пароль по политике или бросить WeakPasswordError.

    Сообщение об ошибке говорит, ЧТО не так, но не повторяет пароль:
    текст ошибки может попасть в лог.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(
            f"Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов"
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise WeakPasswordError(
            f"Пароль должен быть не длиннее {MAX_PASSWORD_LENGTH} символов"
        )
    if not password.strip():
        raise WeakPasswordError("Пароль не может состоять из пробелов")

    folded = password.casefold()
    if folded in _COMMON_PASSWORDS or len(set(folded)) <= 2:
        raise WeakPasswordError("Пароль слишком распространённый, выберите другой")

    if email:
        local_part = email.casefold().split("@", 1)[0]
        if email.casefold() in folded or (
            len(local_part) >= 4 and local_part in folded
        ):
            raise WeakPasswordError("Пароль не должен содержать адрес почты")
