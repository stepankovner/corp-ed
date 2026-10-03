"""Коды приложения-аутентификатора: TOTP, RFC 6238 (ТЗ §3).

SHA-1, 6 цифр, шаг 30 секунд — так работают Яндекс Ключ, Google
Authenticator, 1Password и остальные; другие параметры многие
приложения молча не поддерживают. Принимается шаг до и после текущего
(часы телефона могут отставать). Один и тот же шаг дважды не
принимается — это проверяет вызывающий по totp_last_step.
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

DIGITS = 6
PERIOD = 30
WINDOW = 1
SECRET_BYTES = 20  # 160 бит — как рекомендует RFC 4226 для SHA-1


def new_secret() -> str:
    """Секрет в base32 без «=» — так его ждут приложения."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode().rstrip("=")


def code_at(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 10**DIGITS:0{DIGITS}d}"


def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // PERIOD)


def matching_step(secret: str, code: str, now: float | None = None) -> int | None:
    """Шаг, которому соответствует код, или None. Сравнение — за
    постоянное время для каждого кандидата."""
    code = code.strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    step = current_step(now)
    for candidate in range(step - WINDOW, step + WINDOW + 1):
        if hmac.compare_digest(code_at(secret, candidate), code):
            return candidate
    return None


def provisioning_uri(secret: str, *, account: str, issuer: str = "kronto") -> str:
    """otpauth:// для QR-кода: приложение покажет «kronto (почта)»."""
    label = quote(f"{issuer}:{account}")
    query = urlencode(
        {"secret": secret, "issuer": issuer, "digits": DIGITS, "period": PERIOD}
    )
    return f"otpauth://totp/{label}?{query}"
