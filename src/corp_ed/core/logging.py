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
