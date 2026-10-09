"""Адаптер Outline (облако и своя установка) и совместимого с ним Yonote:
документы коллекций с зеркалом прав по API-ключу администратора.

Написан по OpenAPI Outline и Yonote и коду сервера Outline (09.10) без
живой системы: формы ответов — в тестах
(`tests/connectors/fake_outline.py`), сверить через
`cli connector-check --kind outline` (и `--kind yonote`).
"""

from corp_ed.connectors.outline.adapter import (
    OUTLINE_SPEC,
    YONOTE_SPEC,
    register,
)

__all__ = ["OUTLINE_SPEC", "YONOTE_SPEC", "register"]
