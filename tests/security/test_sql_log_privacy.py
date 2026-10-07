"""Значения из запросов к базе не попадают в текст ошибки и в лог.

Ошибка SQLAlchemy по умолчанию несёт `[parameters: (…)]` — почту, текст
вопроса, хеши, — а Postgres в DETAIL повторяет значение ключа. Трассировка
уходит в лог (unhandled_error), а логи читает больше людей, чем базу.
"""

import json
import os
from types import SimpleNamespace

import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, StatementError

from corp_ed.core import logging as app_logging
from corp_ed.core.database import build_engine, get_engine
from corp_ed.core.logging import REDACTED, configure_logging, mask_sql_values

MARKER = "marker-6f1c@example.ru"


async def test_engine_hides_statement_parameters() -> None:
    engine = build_engine(os.environ["TEST_DATABASE_URL"])
    try:
        async with engine.connect() as connection:
            with pytest.raises(DBAPIError) as caught:
                # Postgres не повторяет значение в тексте ошибки «нет
                # таблицы» — значит, если маркер в тексте, его вписал драйвер.
                await connection.execute(
                    text("SELECT :value FROM no_such_table"), {"value": MARKER}
                )
    finally:
        await engine.dispose()
    assert "no_such_table" in str(caught.value)
    assert MARKER not in str(caught.value)


def test_application_engine_hides_parameters() -> None:
    assert get_engine().sync_engine.hide_parameters is True


TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "x.py", line 1, in <module>\n'
    "sqlalchemy.exc.IntegrityError: (asyncpg.exceptions.UniqueViolationError) "
    'duplicate key value violates unique constraint "uq_accounts_email"\n'
    f"DETAIL:  Key (email)=({MARKER}) already exists.\n"
    "[SQL: INSERT INTO accounts (email, name) VALUES ($1::VARCHAR, $2::VARCHAR)]\n"
    f"[parameters: ('{MARKER}', 'Анна')]\n"
    "(Background on this error at: https://sqlalche.me/e/20/gkpj)"
)


def test_log_masks_sql_parameters_and_key_values() -> None:
    event = {"event": "unhandled_error", "exception": TRACEBACK, "error": TRACEBACK}

    masked = mask_sql_values(None, "error", event)

    for key in ("exception", "error"):
        value = masked[key]
        assert MARKER not in value
        assert "Анна" not in value
        # Что сломалось и где — остаётся: ограничение, запрос, ключ.
        assert 'unique constraint "uq_accounts_email"' in value
        assert "[SQL: INSERT INTO accounts (email, name)" in value
        assert f"[parameters: {REDACTED}]" in value
        assert f"Key (email)={REDACTED}" in value
    assert masked["event"] == "unhandled_error"


def test_log_mask_keeps_other_values() -> None:
    event = {"event": "x", "count": 3, "path": "/api/v1/chat", "note": "Key (b)"}
    assert mask_sql_values(None, "info", dict(event)) == event


def test_logged_exception_has_no_sql_parameters(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Весь конвейер structlog, как в production: JSON-строка в stdout."""
    monkeypatch.setattr(
        app_logging,
        "get_settings",
        lambda: SimpleNamespace(environment="production", debug=False),
    )
    saved = structlog.get_config()
    try:
        configure_logging()
        try:
            raise StatementError(
                "boom", "INSERT INTO t (email) VALUES ($1)", (MARKER,), ValueError()
            )
        except StatementError:
            structlog.get_logger().exception("unhandled_error", path="/x")
    finally:
        structlog.configure(**saved)

    line = capsys.readouterr().out.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["event"] == "unhandled_error"
    assert "INSERT INTO t (email)" in record["exception"]
    assert MARKER not in line
