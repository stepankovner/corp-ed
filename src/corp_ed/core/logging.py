import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

from corp_ed.core.config import get_settings

_SENSITIVE_KEY = re.compile(
    r"password|passwd|secret|token|authorization|api_key|cookie", re.IGNORECASE
)
REDACTED = "[REDACTED]"

# Значения в тексте ошибок базы: `[parameters: (…)]` SQLAlchemy (движок без
# hide_parameters — чужой или будущий) и `Key (email)=(…)` из DETAIL
# Postgres, который asyncpg дописывает в текст исключения. Маскируем до
# конца строки: обрезанный repr параметров не обязан закрыть скобку.
_SQL_PARAMETERS = re.compile(r"\[parameters: [^\n]*")
_SQL_KEY_VALUES = re.compile(r"(Key \([^)\n]*\)=)\([^\n]*")


def redact_sensitive(
    logger: object, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Заменить значения полей с секретами на [REDACTED].

    Второй рубеж: код не должен логировать пароли и токены, но одно
    неосторожное logger.info(..., **data) — и секрет навсегда в логах,
    которые читает больше людей, чем базу.
    """
    for key in list(event_dict):
        if _SENSITIVE_KEY.search(key):
            event_dict[key] = REDACTED
    return event_dict


def mask_sql_values(
    logger: object, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Убрать значения из текста ошибок базы в любом строковом поле.

    Стоит после format_exc_info: трассировка к этому моменту — строка
    в поле exception. Запрос и имя ограничения остаются — по ним ищут
    причину; значения (почта, текст вопроса) — нет.
    """
    for key, value in list(event_dict.items()):
        if not isinstance(value, str):
            continue
        if "[parameters: " not in value and "Key (" not in value:
            continue
        value = _SQL_PARAMETERS.sub(f"[parameters: {REDACTED}]", value)
        event_dict[key] = _SQL_KEY_VALUES.sub(
            lambda match: match.group(1) + REDACTED, value
        )
    return event_dict


def configure_logging() -> None:
    """Настраивает structlog: текст в development, JSON в production."""
    settings = get_settings()
    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        redact_sensitive,
        # Трассировка — строкой внутри записи, а не отдельным выводом:
        # в JSON-логах production она иначе теряется.
        structlog.processors.format_exc_info,
        mask_sql_values,
    ]

    if settings.environment == "production":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.INFO if not settings.debug else logging.DEBUG
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
