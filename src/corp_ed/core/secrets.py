"""Шифрование секретов источников в базе (учётные данные коннекторов).

Зачем, если база и так под паролем: дамп базы, бэкап, SQL-инъекция или
ошибка RLS отдали бы токены к порталам клиентов — ключи от чужих
систем, а не только от нашей. Ключ шифрования живёт в окружении
воркера и API, отдельно от базы: утечка одного без другого бесполезна.

Схема — Fernet (AES-128-CBC + HMAC-SHA256, cryptography): симметрично,
аутентифицировано, с меткой времени. MultiFernet: шифрует первым ключом,
расшифровывает любым — так ротируется ключ без простоя (DEPLOY.md, § 9):
добавить новый первым, перешифровать `cli rotate-connector-secrets`,
убрать старый.

Секреты — словарь строк (токен, refresh-токен, логин); наружу уходит
одна непрозрачная строка. Ни одного пути, по которому расшифрованное
значение попадает в лог или ответ API, быть не должно: расшифровка —
только в момент вызова адаптера.
"""

import json
from collections.abc import Mapping

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from corp_ed.core.exceptions import ServiceUnavailableError

Credentials = dict[str, str]


class SecretsNotConfiguredError(ServiceUnavailableError):
    """CONNECTOR_SECRETS_KEYS не задан: учётные данные некуда положить."""

    def __init__(self) -> None:
        super().__init__()
        self.args = ("Хранение учётных данных источников не настроено",)


class SecretDecryptionError(Exception):
    """Ни один ключ не подошёл: ключ потерян или запись повреждена.

    Признак инцидента, а не ошибки клиента: коннектор останавливается с
    кодом credentials_unreadable, чинит команда.
    """


class SecretBox:
    """Шифрует и расшифровывает словари учётных данных."""

    def __init__(self, keys: list[str]) -> None:
        self._fernet = MultiFernet([Fernet(key) for key in keys]) if keys else None

    @property
    def configured(self) -> bool:
        return self._fernet is not None

    def encrypt(self, credentials: Mapping[str, str]) -> str:
        if self._fernet is None:
            raise SecretsNotConfiguredError()
        if not all(isinstance(v, str) for v in credentials.values()):
            raise TypeError("credentials must be a mapping of strings")
        payload = json.dumps(dict(credentials), ensure_ascii=False, sort_keys=True)
        return self._fernet.encrypt(payload.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> Credentials:
        if self._fernet is None:
            raise SecretsNotConfiguredError()
        try:
            payload = self._fernet.decrypt(token.encode("ascii"))
        except (InvalidToken, UnicodeEncodeError) as exc:
            raise SecretDecryptionError() from exc
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise SecretDecryptionError()
        return {str(k): str(v) for k, v in data.items()}

    def rotate(self, token: str) -> str:
        """Перешифровать действующим (первым) ключом; расшифровка — любым."""
        if self._fernet is None:
            raise SecretsNotConfiguredError()
        try:
            return self._fernet.rotate(token.encode("ascii")).decode("ascii")
        except InvalidToken as exc:
            raise SecretDecryptionError() from exc
