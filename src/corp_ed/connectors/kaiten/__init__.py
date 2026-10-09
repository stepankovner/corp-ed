"""Адаптер Kaiten (облако и коробка): документы и вложения карточек от
имени сотрудника — каждый подключает свой API-токен (режим per_user).

Написан по документации developers.kaiten.ru (09.10) без живой системы:
формы ответов — в тестах (`tests/connectors/fake_kaiten.py`), сверить
с живым Kaiten через `cli connector-check --kind kaiten`.
"""

from corp_ed.connectors.kaiten.adapter import KIND, SPEC, register

__all__ = ["KIND", "SPEC", "register"]
